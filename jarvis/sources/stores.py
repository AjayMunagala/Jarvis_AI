"""Event source storage: in-memory (non-durable) and PostgreSQL (durable) (v7.3).

Seen items are kept per source for deduplication. Every poll refreshes ``last_seen_at`` of the items it still
sees, and only the ``keep`` most recently seen keys are retained, so an item that is still in the feed or folder
is never forgotten and re-notified. Recording a poll's items is one transaction that locks the source row; an
item counts as new only for the poll that inserted it, so two concurrent polls never both notify it.
"""

from datetime import datetime
from time import monotonic
import threading
from typing import Any, Callable, Protocol
from uuid import UUID

from jarvis.persistence.operation_timeout import bounded_connect_timeout, execute_with_deadline, operation_deadline
from jarvis.sources.models import EventSource, SourceConfig, SourceItem


class SourceStorageError(Exception):
    """Sanitized storage failure for event sources."""


class SourceStore(Protocol):
    durable: bool

    def save_source(self, source: EventSource, *, max_sources: int) -> bool: ...
    def get_source(self, owner_id: UUID, source_id: UUID) -> EventSource | None: ...
    def list_sources(self, owner_id: UUID, *, limit: int = 50, offset: int = 0) -> tuple[list[EventSource], int]: ...
    def update_settings(self, source: EventSource) -> EventSource | None: ...
    def record_state(self, owner_id: UUID, source_id: UUID, *, last_polled_at: datetime,
                     last_success_at: datetime | None, consecutive_failures: int, last_error: str | None,
                     backoff_until: datetime | None, disable: bool, now: datetime) -> bool: ...
    def delete_source(self, owner_id: UUID, source_id: UUID) -> bool: ...
    def record_items(self, owner_id: UUID, source_id: UUID, items: list[SourceItem], *, now: datetime,
                     keep: int) -> set[str] | None: ...
    def list_items(self, owner_id: UUID, source_id: UUID, *, limit: int) -> list[SourceItem]: ...


def _apply_state(source: EventSource, *, last_polled_at, last_success_at, consecutive_failures, last_error,
                 backoff_until, disable, now) -> EventSource:
    update: dict[str, Any] = {"last_polled_at": last_polled_at, "last_success_at": last_success_at,
                              "consecutive_failures": consecutive_failures, "last_error": last_error,
                              "backoff_until": backoff_until}
    if disable:
        update.update(enabled=False, updated_at=now)
    return source.model_copy(update=update)


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

    def update_settings(self, source: EventSource) -> EventSource | None:
        with self._lock:
            current = self.get_source(source.owner_id, source.id)
            if current is None:
                return None
            updated = current.model_copy(update={
                "name": source.name, "enabled": source.enabled, "poll_interval_minutes": source.poll_interval_minutes,
                "importance": source.importance, "max_items_per_poll": source.max_items_per_poll,
                "include_keywords": list(source.include_keywords), "exclude_keywords": list(source.exclude_keywords),
                "consecutive_failures": source.consecutive_failures, "backoff_until": source.backoff_until,
                "updated_at": source.updated_at})
            self._sources[source.id] = updated
            return updated

    def record_state(self, owner_id, source_id, *, last_polled_at, last_success_at, consecutive_failures, last_error,
                     backoff_until, disable, now) -> bool:
        with self._lock:
            current = self.get_source(owner_id, source_id)
            if current is None:
                return False
            self._sources[source_id] = _apply_state(
                current, last_polled_at=last_polled_at, last_success_at=last_success_at,
                consecutive_failures=consecutive_failures, last_error=last_error, backoff_until=backoff_until,
                disable=disable, now=now)
            return True

    def delete_source(self, owner_id: UUID, source_id: UUID) -> bool:
        with self._lock:
            if self.get_source(owner_id, source_id) is None:
                return False
            del self._sources[source_id]
            self._items.pop(source_id, None)
            return True

    def record_items(self, owner_id, source_id, items, *, now, keep):
        with self._lock:
            if self.get_source(owner_id, source_id) is None:
                return None
            seen = self._items.setdefault(source_id, {})
            inserted: set[str] = set()
            for item in items:
                existing = seen.get(item.item_key)
                if existing is None:
                    seen[item.item_key] = item
                    inserted.add(item.item_key)
                else:
                    seen[item.item_key] = existing.model_copy(update={"last_seen_at": now})
            if len(seen) > keep:
                ordered = sorted(seen.values(), key=lambda item: (item.last_seen_at, item.first_seen_at, item.item_key),
                                 reverse=True)
                for item in ordered[keep:]:
                    del seen[item.item_key]
            return inserted

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
                   "last_success_at, consecutive_failures, last_error, backoff_until, created_at, updated_at")
_ITEM_COLUMNS = "source_id, owner_id, item_key, title, link, published_at, summary, matched, first_seen_at, last_seen_at"


def _source(row) -> EventSource:
    return EventSource(
        id=row[0], owner_id=row[1], kind=row[2], name=row[3],
        config=SourceConfig(url=row[4], path=row[5], recursive_depth=row[6]), enabled=row[7],
        poll_interval_minutes=row[8], importance=row[9], include_keywords=list(row[10]),
        exclude_keywords=list(row[11]), max_items_per_poll=row[12], last_polled_at=row[13], last_success_at=row[14],
        consecutive_failures=row[15], last_error=row[16], backoff_until=row[17], created_at=row[18],
        updated_at=row[19])


def _item(row) -> SourceItem:
    return SourceItem(source_id=row[0], owner_id=row[1], item_key=row[2], title=row[3], link=row[4],
                      published_at=row[5], summary=row[6], matched=row[7], first_seen_at=row[8], last_seen_at=row[9])


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
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (source.id, source.owner_id, source.kind, source.name, source.config.url, source.config.path,
                     source.config.recursive_depth, source.enabled, source.poll_interval_minutes, source.importance,
                     list(source.include_keywords), list(source.exclude_keywords), source.max_items_per_poll,
                     source.last_polled_at, source.last_success_at, source.consecutive_failures, source.last_error,
                     source.backoff_until, source.created_at, source.updated_at))
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

    def update_settings(self, source: EventSource) -> EventSource | None:
        row = self._run(lambda execute: execute(
            f"""UPDATE jarvis_app.event_sources SET name = %s, enabled = %s, poll_interval_minutes = %s,
                   importance = %s, max_items_per_poll = %s, include_keywords = %s, exclude_keywords = %s,
                   consecutive_failures = %s, backoff_until = %s, updated_at = %s
               WHERE id = %s AND owner_id = %s RETURNING {_SOURCE_COLUMNS}""",
            (source.name, source.enabled, source.poll_interval_minutes, source.importance, source.max_items_per_poll,
             list(source.include_keywords), list(source.exclude_keywords), source.consecutive_failures,
             source.backoff_until, source.updated_at, source.id, source.owner_id)).fetchone(),
            "Failed to update the event source.")
        return _source(row) if row else None

    def record_state(self, owner_id, source_id, *, last_polled_at, last_success_at, consecutive_failures, last_error,
                     backoff_until, disable, now) -> bool:
        return self._run(lambda execute: execute(
            """UPDATE jarvis_app.event_sources SET last_polled_at = %s, last_success_at = %s,
                   consecutive_failures = %s, last_error = %s, backoff_until = %s,
                   enabled = CASE WHEN %s THEN false ELSE enabled END,
                   updated_at = CASE WHEN %s THEN %s ELSE updated_at END
               WHERE id = %s AND owner_id = %s""",
            (last_polled_at, last_success_at, consecutive_failures, last_error, backoff_until, disable, disable, now,
             source_id, owner_id)).rowcount == 1, "Failed to record the poll state.")

    def delete_source(self, owner_id: UUID, source_id: UUID) -> bool:
        return self._run(lambda execute: execute(
            "DELETE FROM jarvis_app.event_sources WHERE id = %s AND owner_id = %s",
            (source_id, owner_id)).rowcount == 1, "Failed to delete the event source.")

    def record_items(self, owner_id, source_id, items, *, now, keep):
        def work(execute):
            if execute("SELECT 1 FROM jarvis_app.event_sources WHERE id = %s AND owner_id = %s FOR UPDATE",
                       (source_id, owner_id)).fetchone() is None:
                return None
            inserted: set[str] = set()
            if items:
                rows = execute(
                    f"""INSERT INTO jarvis_app.event_source_items ({_ITEM_COLUMNS})
                        SELECT %s, %s, key, title, link, published, summary, matched, %s, %s
                        FROM unnest(%s::text[], %s::text[], %s::text[], %s::timestamptz[], %s::text[], %s::boolean[])
                             AS batch(key, title, link, published, summary, matched)
                        ON CONFLICT (source_id, item_key) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at
                        RETURNING item_key, (xmax = 0) AS inserted""",
                    (source_id, owner_id, now, now, [item.item_key for item in items], [item.title for item in items],
                     [item.link for item in items], [item.published_at for item in items],
                     [item.summary for item in items], [item.matched for item in items])).fetchall()
                inserted = {row[0] for row in rows if row[1]}
            execute("""DELETE FROM jarvis_app.event_source_items WHERE source_id = %s AND item_key IN (
                           SELECT item_key FROM jarvis_app.event_source_items WHERE source_id = %s
                           ORDER BY last_seen_at DESC, first_seen_at DESC, item_key DESC OFFSET %s)""",
                    (source_id, source_id, keep))
            return inserted

        return self._run(work, "Failed to record the source items.")

    def list_items(self, owner_id, source_id, *, limit):
        rows = self._run(lambda execute: execute(
            f"""SELECT {_ITEM_COLUMNS} FROM jarvis_app.event_source_items WHERE source_id = %s AND owner_id = %s
                ORDER BY first_seen_at DESC, published_at DESC NULLS LAST, item_key DESC LIMIT %s""",
            (source_id, owner_id, limit)).fetchall(), "Failed to list the source items.")
        return [_item(row) for row in rows]
