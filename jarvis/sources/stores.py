"""Event source storage: in-memory (non-durable) and PostgreSQL (durable) (v7.3).

Seen items are kept per source for deduplication. Every poll refreshes ``last_seen_at`` of the items it still
sees, and only the ``keep`` most recently seen keys are retained, so an item that is still in the feed or folder
is never forgotten and re-reported.

``sync_items`` is one transaction that locks the source row: whether this is the first (baseline) poll, which
items are new and which folder files changed are all decided under that lock (``plan_sync``), so two polls of
the same source, even from two processes, never both report an item. Failures are counted in SQL
(``consecutive_failures + 1``) and the turned-off notice is due only to the update that turned the source off.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import monotonic
import threading
from typing import Any, Callable, Protocol
from uuid import UUID

from jarvis.persistence.operation_timeout import bounded_connect_timeout, execute_with_deadline, operation_deadline
from jarvis.sources.models import EventSource, SourceConfig, SourceItem

MAX_BACKOFF = timedelta(hours=24)


class SourceStorageError(Exception):
    """Sanitized storage failure for event sources."""


def backoff_for(failures: int, interval_minutes: int) -> timedelta:
    """Exponential: twice the interval after one failure, four times after two, ..., at most 24 hours."""
    return min(MAX_BACKOFF, timedelta(minutes=interval_minutes) * (2 ** min(failures, 16)))


@dataclass
class SyncResult:
    baseline: bool
    new: list[SourceItem] = field(default_factory=list)
    changed: list[SourceItem] = field(default_factory=list)
    pending: int = 0


@dataclass(frozen=True)
class FailureOutcome:
    consecutive_failures: int
    backoff_until: datetime
    turned_off: bool  # this update turned the source off (true -> false)


def plan_sync(existing: dict[str, SourceItem], observed: list[SourceItem], *, baseline: bool, now: datetime,
              interval_minutes: int) -> tuple[list[SourceItem], SyncResult]:
    """Rows to write and what to report. ``observed`` items carry ``version`` for folder files, None for feeds."""
    result = SyncResult(baseline=baseline)
    rows: list[SourceItem] = []
    quiet = timedelta(minutes=interval_minutes)
    for item in observed:
        prev = existing.get(item.item_key)
        if item.version is None:  # feed item: new once, then only refreshed
            if prev is None:
                rows.append(item.model_copy(update={"first_seen_at": now, "last_seen_at": now}))
                if not baseline:
                    result.new.append(rows[-1])
            else:
                rows.append(prev.model_copy(update={"last_seen_at": now}))
            continue
        if prev is None:  # a file seen for the first time: settled at the baseline, else reported once stable
            rows.append(item.model_copy(update={"first_seen_at": now, "last_seen_at": now,
                                                "settled_version": item.version if baseline else None}))
            result.pending += not baseline
            continue
        current = prev.model_copy(update={"title": item.title, "summary": item.summary,
                                          "published_at": item.published_at, "matched": item.matched,
                                          "last_seen_at": now, "version": item.version})
        if prev.version != item.version:  # still being written or just changed: wait for one more poll
            rows.append(current)
            result.pending += item.version != prev.settled_version
            continue
        if item.version == prev.settled_version:
            rows.append(current)
            continue
        if prev.notified_at is not None and now - prev.notified_at < quiet:  # at most once per poll interval
            rows.append(current)
            result.pending += 1
            continue
        settled = current.model_copy(update={"settled_version": item.version, "notified_at": now})
        rows.append(settled)
        (result.new if prev.settled_version is None else result.changed).append(settled)
    return rows, result


def _prune(seen: dict[str, SourceItem], keep: int) -> None:
    if len(seen) > keep:
        ordered = sorted(seen.values(), key=lambda item: (item.last_seen_at, item.first_seen_at, item.item_key),
                         reverse=True)
        for item in ordered[keep:]:
            del seen[item.item_key]


class SourceStore(Protocol):
    durable: bool

    def save_source(self, source: EventSource, *, max_sources: int) -> bool: ...
    def get_source(self, owner_id: UUID, source_id: UUID) -> EventSource | None: ...
    def list_sources(self, owner_id: UUID, *, limit: int = 50, offset: int = 0) -> tuple[list[EventSource], int]: ...
    def update_settings(self, source: EventSource, *, reset_failures: bool) -> EventSource | None: ...
    def delete_source(self, owner_id: UUID, source_id: UUID) -> bool: ...
    def sync_items(self, owner_id: UUID, source_id: UUID, observed: list[SourceItem], *, now: datetime, keep: int,
                   interval_minutes: int) -> SyncResult | None: ...
    def record_success(self, owner_id: UUID, source_id: UUID, *, now: datetime,
                       notified_at: datetime | None) -> bool: ...
    def record_failure(self, owner_id: UUID, source_id: UUID, *, now: datetime, error: str,
                       disable_after: int) -> FailureOutcome | None: ...
    def list_items(self, owner_id: UUID, source_id: UUID, *, limit: int) -> list[SourceItem]: ...


class MemorySourceStore:
    durable = False

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sources: dict[UUID, EventSource] = {}
        self._items: dict[UUID, dict[str, SourceItem]] = {}

    def save_source(self, source: EventSource, *, max_sources: int) -> bool:
        with self._lock:
            if sum(1 for item in self._sources.values() if item.owner_id == source.owner_id) >= max_sources:
                return False
            self._sources[source.id] = source
            self._items[source.id] = {}
            return True

    def get_source(self, owner_id: UUID, source_id: UUID) -> EventSource | None:
        with self._lock:
            source = self._sources.get(source_id)
            return source if source is not None and source.owner_id == owner_id else None

    def list_sources(self, owner_id, *, limit=50, offset=0):
        with self._lock:
            owned = sorted((item for item in self._sources.values() if item.owner_id == owner_id),
                           key=lambda item: (item.created_at, str(item.id)), reverse=True)
            return owned[offset:offset + limit], len(owned)

    def update_settings(self, source: EventSource, *, reset_failures: bool) -> EventSource | None:
        with self._lock:
            current = self.get_source(source.owner_id, source.id)
            if current is None:
                return None
            update = {"name": source.name, "enabled": source.enabled,
                      "poll_interval_minutes": source.poll_interval_minutes, "importance": source.importance,
                      "max_items_per_poll": source.max_items_per_poll,
                      "include_keywords": list(source.include_keywords),
                      "exclude_keywords": list(source.exclude_keywords), "updated_at": source.updated_at}
            if reset_failures:
                update.update(consecutive_failures=0, backoff_until=None, last_error=None)
            self._sources[source.id] = updated = current.model_copy(update=update)
            return updated

    def delete_source(self, owner_id: UUID, source_id: UUID) -> bool:
        with self._lock:
            if self.get_source(owner_id, source_id) is None:
                return False
            del self._sources[source_id]
            self._items.pop(source_id, None)
            return True

    def sync_items(self, owner_id, source_id, observed, *, now, keep, interval_minutes):
        with self._lock:
            source = self.get_source(owner_id, source_id)
            if source is None:
                return None
            seen = self._items.setdefault(source_id, {})
            rows, result = plan_sync(seen, observed, baseline=source.last_success_at is None, now=now,
                                     interval_minutes=interval_minutes)
            for row in rows:
                seen[row.item_key] = row
            _prune(seen, keep)
            return result

    def record_success(self, owner_id, source_id, *, now, notified_at) -> bool:
        with self._lock:
            current = self.get_source(owner_id, source_id)
            if current is None:
                return False
            self._sources[source_id] = current.model_copy(update={
                "last_polled_at": now, "last_success_at": now, "consecutive_failures": 0, "last_error": None,
                "backoff_until": None, "last_notified_at": notified_at or current.last_notified_at})
            return True

    def record_failure(self, owner_id, source_id, *, now, error, disable_after):
        with self._lock:
            current = self.get_source(owner_id, source_id)
            if current is None:
                return None
            failures = current.consecutive_failures + 1
            backoff_until = now + backoff_for(failures, current.poll_interval_minutes)
            turn_off = current.enabled and failures >= disable_after
            update: dict[str, Any] = {"last_polled_at": now, "consecutive_failures": failures, "last_error": error,
                                      "backoff_until": backoff_until}
            if turn_off:
                update.update(enabled=False, updated_at=now)
            self._sources[source_id] = current.model_copy(update=update)
            return FailureOutcome(failures, backoff_until, turn_off)

    def list_items(self, owner_id, source_id, *, limit):
        with self._lock:
            if self.get_source(owner_id, source_id) is None:
                return []
            ordered = sorted(self._items.get(source_id, {}).values(),
                             key=lambda item: (item.first_seen_at, item.published_at or item.first_seen_at,
                                               item.item_key), reverse=True)
            return ordered[:limit]


_SOURCE_COLUMNS = ("id, owner_id, kind, name, url, folder_path, recursive_depth, enabled, poll_interval_minutes, "
                   "importance, include_keywords, exclude_keywords, max_items_per_poll, last_polled_at, "
                   "last_success_at, consecutive_failures, last_error, backoff_until, last_notified_at, created_at, "
                   "updated_at")
_ITEM_COLUMNS = ("source_id, owner_id, item_key, title, link, published_at, summary, matched, first_seen_at, "
                 "last_seen_at, version, settled_version, notified_at")


def _source(row) -> EventSource:
    return EventSource(
        id=row[0], owner_id=row[1], kind=row[2], name=row[3],
        config=SourceConfig(url=row[4], path=row[5], recursive_depth=row[6]), enabled=row[7],
        poll_interval_minutes=row[8], importance=row[9], include_keywords=list(row[10]),
        exclude_keywords=list(row[11]), max_items_per_poll=row[12], last_polled_at=row[13], last_success_at=row[14],
        consecutive_failures=row[15], last_error=row[16], backoff_until=row[17], last_notified_at=row[18],
        created_at=row[19], updated_at=row[20])


def _item(row) -> SourceItem:
    return SourceItem(source_id=row[0], owner_id=row[1], item_key=row[2], title=row[3], link=row[4],
                      published_at=row[5], summary=row[6], matched=row[7], first_seen_at=row[8], last_seen_at=row[9],
                      version=row[10], settled_version=row[11], notified_at=row[12])


class PostgresSourceStore:
    durable = True

    def __init__(self, database_url: str, *, connect_timeout_seconds: int = 3, operation_timeout_seconds: int = 5,
                 connect: Callable | None = None) -> None:
        if connect is None:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError("PostgreSQL storage requires psycopg.") from exc
            connect = psycopg.connect
        self._database_url = database_url
        self._connect = connect
        self._connect_timeout = connect_timeout_seconds
        self._operation_timeout = operation_timeout_seconds

    def _run(self, work: Callable[[Callable[..., Any]], Any], failure: str) -> Any:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        try:
            timeout = bounded_connect_timeout(self._connect_timeout, deadline, clock=monotonic)
            with self._connect(self._database_url, connect_timeout=timeout) as conn:
                return work(lambda sql, params=(): execute_with_deadline(conn, deadline, sql, params, clock=monotonic))
        except Exception as exc:  # noqa: BLE001 - sanitized: no driver message, SQL or URL is exposed
            raise SourceStorageError(failure) from exc

    def save_source(self, source: EventSource, *, max_sources: int) -> bool:
        def work(execute):
            execute("SELECT pg_advisory_xact_lock(hashtextextended('jarvis.event_sources:' || %s::text, 0))",
                    (source.owner_id,))
            if execute("SELECT count(*) FROM jarvis_app.event_sources WHERE owner_id = %s",
                       (source.owner_id,)).fetchone()[0] >= max_sources:
                return False
            execute(f"INSERT INTO jarvis_app.event_sources ({_SOURCE_COLUMNS}) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (source.id, source.owner_id, source.kind, source.name, source.config.url, source.config.path,
                     source.config.recursive_depth, source.enabled, source.poll_interval_minutes, source.importance,
                     list(source.include_keywords), list(source.exclude_keywords), source.max_items_per_poll,
                     source.last_polled_at, source.last_success_at, source.consecutive_failures, source.last_error,
                     source.backoff_until, source.last_notified_at, source.created_at, source.updated_at))
            return True

        return self._run(work, "Failed to store the event source.")

    def get_source(self, owner_id: UUID, source_id: UUID) -> EventSource | None:
        row = self._run(lambda execute: execute(
            f"SELECT {_SOURCE_COLUMNS} FROM jarvis_app.event_sources WHERE id = %s AND owner_id = %s",
            (source_id, owner_id)).fetchone(), "Failed to read the event source.")
        return _source(row) if row else None

    def list_sources(self, owner_id, *, limit=50, offset=0):
        def work(execute):
            total = execute("SELECT count(*) FROM jarvis_app.event_sources WHERE owner_id = %s",
                            (owner_id,)).fetchone()[0]
            rows = execute(f"SELECT {_SOURCE_COLUMNS} FROM jarvis_app.event_sources WHERE owner_id = %s "
                           "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s", (owner_id, limit, offset)).fetchall()
            return [_source(row) for row in rows], int(total)

        return self._run(work, "Failed to list event sources.")

    def update_settings(self, source: EventSource, *, reset_failures: bool) -> EventSource | None:
        # Poll state (failures, backoff, last error) is written only when the owner turns the source back on, so an
        # edit never overwrites a failure counted by a concurrent poll.
        row = self._run(lambda execute: execute(
            f"""UPDATE jarvis_app.event_sources SET name = %s, enabled = %s, poll_interval_minutes = %s,
                   importance = %s, max_items_per_poll = %s, include_keywords = %s, exclude_keywords = %s,
                   consecutive_failures = CASE WHEN %s THEN 0 ELSE consecutive_failures END,
                   backoff_until = CASE WHEN %s THEN NULL ELSE backoff_until END,
                   last_error = CASE WHEN %s THEN NULL ELSE last_error END,
                   updated_at = %s
               WHERE id = %s AND owner_id = %s RETURNING {_SOURCE_COLUMNS}""",
            (source.name, source.enabled, source.poll_interval_minutes, source.importance, source.max_items_per_poll,
             list(source.include_keywords), list(source.exclude_keywords), reset_failures, reset_failures,
             reset_failures, source.updated_at, source.id, source.owner_id)).fetchone(),
            "Failed to update the event source.")
        return _source(row) if row else None

    def delete_source(self, owner_id: UUID, source_id: UUID) -> bool:
        return self._run(lambda execute: execute(
            "DELETE FROM jarvis_app.event_sources WHERE id = %s AND owner_id = %s",
            (source_id, owner_id)).rowcount == 1, "Failed to delete the event source.")

    def sync_items(self, owner_id, source_id, observed, *, now, keep, interval_minutes):
        def work(execute):
            row = execute("SELECT last_success_at FROM jarvis_app.event_sources WHERE id = %s AND owner_id = %s "
                          "FOR UPDATE", (source_id, owner_id)).fetchone()
            if row is None:
                return None
            keys = [item.item_key for item in observed]
            existing = {}
            if keys:
                existing = {item.item_key: item for item in map(_item, execute(
                    f"SELECT {_ITEM_COLUMNS} FROM jarvis_app.event_source_items "
                    "WHERE source_id = %s AND item_key = ANY(%s)", (source_id, keys)).fetchall())}
            rows, result = plan_sync(existing, observed, baseline=row[0] is None, now=now,
                                     interval_minutes=interval_minutes)
            if rows:
                execute(
                    f"""INSERT INTO jarvis_app.event_source_items ({_ITEM_COLUMNS})
                        SELECT %s, %s, * FROM unnest(%s::text[], %s::text[], %s::text[], %s::timestamptz[],
                             %s::text[], %s::boolean[], %s::timestamptz[], %s::timestamptz[], %s::text[],
                             %s::text[], %s::timestamptz[])
                        ON CONFLICT (source_id, item_key) DO UPDATE SET title = EXCLUDED.title,
                            link = EXCLUDED.link, published_at = EXCLUDED.published_at, summary = EXCLUDED.summary,
                            matched = EXCLUDED.matched, last_seen_at = EXCLUDED.last_seen_at,
                            version = EXCLUDED.version, settled_version = EXCLUDED.settled_version,
                            notified_at = EXCLUDED.notified_at""",
                    (source_id, owner_id, [r.item_key for r in rows], [r.title for r in rows],
                     [r.link for r in rows], [r.published_at for r in rows], [r.summary for r in rows],
                     [r.matched for r in rows], [r.first_seen_at for r in rows], [r.last_seen_at for r in rows],
                     [r.version for r in rows], [r.settled_version for r in rows], [r.notified_at for r in rows]))
            execute("""DELETE FROM jarvis_app.event_source_items WHERE source_id = %s AND item_key IN (
                           SELECT item_key FROM jarvis_app.event_source_items WHERE source_id = %s
                           ORDER BY last_seen_at DESC, first_seen_at DESC, item_key DESC OFFSET %s)""",
                    (source_id, source_id, keep))
            return result

        return self._run(work, "Failed to record the source items.")

    def record_success(self, owner_id, source_id, *, now, notified_at) -> bool:
        return self._run(lambda execute: execute(
            """UPDATE jarvis_app.event_sources SET last_polled_at = %s, last_success_at = %s,
                   consecutive_failures = 0, last_error = NULL, backoff_until = NULL,
                   last_notified_at = COALESCE(%s, last_notified_at)
               WHERE id = %s AND owner_id = %s""",
            (now, now, notified_at, source_id, owner_id)).rowcount == 1, "Failed to record the poll state.")

    def record_failure(self, owner_id, source_id, *, now, error, disable_after):
        row = self._run(lambda execute: execute(
            """WITH previous AS (
                   SELECT id, enabled FROM jarvis_app.event_sources WHERE id = %s AND owner_id = %s FOR UPDATE)
               UPDATE jarvis_app.event_sources AS s SET
                   last_polled_at = %s, consecutive_failures = s.consecutive_failures + 1, last_error = %s,
                   backoff_until = %s + LEAST(make_interval(mins => s.poll_interval_minutes)
                                              * power(2, LEAST(s.consecutive_failures + 1, 16)),
                                              interval '24 hours'),
                   enabled = CASE WHEN s.consecutive_failures + 1 >= %s THEN false ELSE s.enabled END,
                   updated_at = CASE WHEN s.enabled AND s.consecutive_failures + 1 >= %s THEN %s ELSE s.updated_at END
               FROM previous WHERE s.id = previous.id
               RETURNING s.consecutive_failures, s.backoff_until, previous.enabled AND NOT s.enabled""",
            (source_id, owner_id, now, error, now, disable_after, disable_after, now)).fetchone(),
            "Failed to record the poll state.")
        return FailureOutcome(int(row[0]), row[1], bool(row[2])) if row else None

    def list_items(self, owner_id, source_id, *, limit):
        rows = self._run(lambda execute: execute(
            f"""SELECT {_ITEM_COLUMNS} FROM jarvis_app.event_source_items WHERE source_id = %s AND owner_id = %s
                ORDER BY first_seen_at DESC, published_at DESC NULLS LAST, item_key DESC LIMIT %s""",
            (source_id, owner_id, limit)).fetchall(), "Failed to list the source items.")
        return [_item(row) for row in rows]
