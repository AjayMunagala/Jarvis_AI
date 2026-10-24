"""Opt-in event sources (v7.3): poll RSS/Atom feeds and watched folders, deduplicate, filter, notify.

Opt-in: nothing is fetched or watched until the owner creates a source. Sources are polled by the scheduler's
background runner (off unless ``JARVIS_SCHEDULER_RUNNER_ENABLED=true``) or when the owner asks
(``POST /sources/{id}/poll``).

Per poll and source:
- the first successful poll records the existing items as seen without notifying (no flood on subscribe);
- later polls report items not seen before that pass the keyword filters (include any / exclude any,
  case-insensitive); folder files are keyed by path, and a new or changed file is reported once it is unchanged
  across two polls (a file still being written is not reported) and at most once per poll interval;
- at most ONE notification per poll and source: the item itself when exactly one is reported, otherwise a summary
  ("N new items from <source>") listing the first ``max_items_per_poll`` titles; high importance follows the same
  rule;
- notifications go through ``SchedulerService.dispatch_notification`` (category ``"source"``, priority from the
  source's importance, never ``urgent``), so quiet hours and the held-duplicate check apply. The anti-fatigue
  throttle is keyed per source (``source:<id>``, with the source's persisted ``last_notified_at``), so one chatty
  source cannot hide another; high importance bypasses it. A suppressed notification is counted in the poll
  result and the items stay listed under ``GET /sources/{id}/items``.

Runtime: the runner's hook (``poll_due``) only starts a background cycle and returns at once, so polling never
delays reminders. A cycle polls due sources on at most ``max_concurrent_polls`` (4) threads within a time budget
(25 s); sources not started in time carry over to the next tick. Each fetch runs in its own thread with a hard
timeout; a timed-out fetch thread cannot be killed, so at most ``max_abandoned_fetches`` (8) may be outstanding,
and while that many are, new fetches are refused (reported in the poll result and the status). ``close`` stops
new polls and waits for the running ones.

Isolation: every source has its own error handling; failures back off exponentially (capped at 24 h, counted in
the store atomically); after 10 failures in a row the source is turned off and the owner is told once. If even
recording a result fails, an in-memory next-eligible time keeps the source from being re-fetched every tick.
"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import threading
from pathlib import Path
from time import monotonic
from typing import Any, Callable
from uuid import UUID, uuid4

from jarvis.research.parsers import apply_injection_fence
from jarvis.sources.fetchers import (
    FetchError, HttpFeedFetcher, Resolver, check_feed_url, resolve_watched_folder, scan_folder,
)
from jarvis.sources.models import (
    EventSource, EventSourcePage, PollResult, SourceCreateRequest, SourceItem, SourceItemPage, SourceUpdateRequest,
)
from jarvis.sources.parsers import FeedParseError, parse_feed_document
from jarvis.sources.stores import MAX_BACKOFF, SourceStorageError, SourceStore, SyncResult, backoff_for

logger = logging.getLogger(__name__)

MAX_SOURCES_PER_OWNER = 50
KEEP_ITEMS_PER_SOURCE = 1000
MAX_LISTED_ITEMS = 50
AUTO_DISABLE_AFTER_FAILURES = 10
MAX_SOURCES_PER_TICK = 10
MAX_CONCURRENT_POLLS = 4
TICK_BUDGET_SECONDS = 25.0
MAX_ABANDONED_FETCHES = 8
HARD_TIMEOUT_SECONDS = 20.0  # the fetcher's own limit is 15 s; this also bounds DNS resolution and folder scans
NOTIFICATION_CATEGORY = "source"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class SourceNotFound(Exception):
    pass


class SourceInvalid(Exception):
    pass


class SourceConflict(Exception):
    pass


def _key(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def keyword_match(text: str, include: list[str], exclude: list[str]) -> bool:
    folded = text.casefold()
    if include and not any(term.casefold() in folded for term in include):
        return False
    return not any(term.casefold() in folded for term in exclude)


def fence_item_for_model(source: EventSource, item: SourceItem) -> str:
    """Item text for a model prompt, inside the untrusted-content fence used for documents."""
    body = "\n".join(part for part in (item.title, item.link or "", item.summary or "") if part)
    return apply_injection_fence(f"source:{source.name}", f"source/{source.kind}", body)


def _clean_error(exc: BaseException) -> str:
    if isinstance(exc, (FetchError, FeedParseError)):
        return str(exc)[:200] or "The source could not be read."
    return f"Unexpected error ({type(exc).__name__})."[:200]


class _Busy(Exception):
    """No fetch was started (too many timed-out fetch threads are still running, or the service is closing)."""


class SourcesService:
    def __init__(self, store: SourceStore, *, scheduler_service: Any = None, audit_service: Any = None,
                 feed_fetcher: Callable[[str], bytes] | None = None, resolver: Resolver | None = None,
                 folder_roots: tuple[Path, ...] = (), hard_timeout_seconds: float = HARD_TIMEOUT_SECONDS) -> None:
        self.store = store
        self.scheduler_service = scheduler_service
        self.audit_service = audit_service
        self.resolver = resolver
        self.feed_fetcher = feed_fetcher or HttpFeedFetcher(**({"resolver": resolver} if resolver else {}))
        self.folder_roots = tuple(folder_roots)
        self.hard_timeout_seconds = hard_timeout_seconds
        self.max_sources_per_tick = MAX_SOURCES_PER_TICK
        self.max_concurrent_polls = MAX_CONCURRENT_POLLS
        self.tick_budget_seconds = TICK_BUDGET_SECONDS
        self.max_abandoned_fetches = MAX_ABANDONED_FETCHES
        self.cycles = 0
        self.last_cycle: dict[str, int] | None = None
        self.truncated_sources: set[UUID] = set()
        self._polling: set[UUID] = set()
        self._polling_lock = threading.Lock()
        self._abandoned: set[threading.Thread] = set()
        self._next_eligible: dict[UUID, datetime] = {}
        self._dispatcher: threading.Thread | None = None
        self._dispatch_lock = threading.Lock()
        self._closing = threading.Event()

    # ------------------------------------------------------------------ audit

    def _audit(self, owner_id: UUID, event_type: str, subject: str, details: dict[str, Any],
               status: str = "succeeded") -> None:
        if self.audit_service is None:
            return
        try:
            self.audit_service.record(owner_id=owner_id, category="scheduler", event_type=event_type,
                                      decision="allowed", status=status, subject=subject[:200], details=details)
        except Exception as exc:  # noqa: BLE001 - auditing must not break a poll
            logger.warning("Failed to record source audit event: %s", type(exc).__name__)

    # ------------------------------------------------------------------ CRUD

    def create_source(self, owner_id: UUID, req: SourceCreateRequest, now: datetime | None = None) -> EventSource:
        now = now or datetime.now(timezone.utc)
        config = req.config
        try:
            if req.kind == "rss":
                config = config.model_copy(update={"url": check_feed_url(config.url or "")})
            else:
                resolve_watched_folder(config.path or "", self.folder_roots)
        except FetchError as exc:
            raise SourceInvalid(str(exc)) from None
        source = EventSource(
            id=uuid4(), owner_id=owner_id, kind=req.kind, name=req.name, config=config, enabled=req.enabled,
            poll_interval_minutes=req.poll_interval_minutes, importance=req.importance,
            include_keywords=req.include_keywords, exclude_keywords=req.exclude_keywords,
            max_items_per_poll=req.max_items_per_poll, created_at=now, updated_at=now)
        if not self.store.save_source(source, max_sources=MAX_SOURCES_PER_OWNER):
            raise SourceConflict(f"At most {MAX_SOURCES_PER_OWNER} sources are allowed; delete one first.")
        self._audit(owner_id, "sources.create", f"sources:{source.id}", {"kind": source.kind})
        return source

    def get_source(self, owner_id: UUID, source_id: UUID) -> EventSource:
        source = self.store.get_source(owner_id, source_id)
        if source is None:
            raise SourceNotFound("The source was not found.")
        return source

    def list_sources(self, owner_id: UUID, limit: int = 50, offset: int = 0) -> EventSourcePage:
        items, total = self.store.list_sources(owner_id, limit=max(1, min(limit, 50)), offset=max(0, offset))
        return EventSourcePage(items=items, total=total)

    def update_source(self, owner_id: UUID, source_id: UUID, req: SourceUpdateRequest,
                      now: datetime | None = None) -> EventSource:
        now = now or datetime.now(timezone.utc)
        current = self.get_source(owner_id, source_id)
        changes = {key: value for key, value in req.model_dump(exclude_unset=True).items() if value is not None}
        reenable = bool(changes.get("enabled")) and not current.enabled
        if current.kind == "folder" and changes.get("enabled"):
            try:
                resolve_watched_folder(current.config.path or "", self.folder_roots)
            except FetchError as exc:
                raise SourceInvalid(str(exc)) from None
        # Turning a source back on (for example after it was turned off for failing) starts afresh.
        updated = self.store.update_settings(current.model_copy(update={**changes, "updated_at": now}),
                                             reset_failures=reenable)
        if updated is None:
            raise SourceNotFound("The source was not found.")
        if reenable:
            self._next_eligible.pop(source_id, None)
        self._audit(owner_id, "sources.update", f"sources:{source_id}",
                    {"enabled": updated.enabled, "fields": len(req.model_fields_set)})
        return updated

    def delete_source(self, owner_id: UUID, source_id: UUID) -> None:
        if not self.store.delete_source(owner_id, source_id):
            raise SourceNotFound("The source was not found.")
        self._next_eligible.pop(source_id, None)
        self.truncated_sources.discard(source_id)
        self._audit(owner_id, "sources.delete", f"sources:{source_id}", {"deleted": True})

    def list_items(self, owner_id: UUID, source_id: UUID, limit: int = MAX_LISTED_ITEMS) -> SourceItemPage:
        self.get_source(owner_id, source_id)
        return SourceItemPage(items=self.store.list_items(owner_id, source_id,
                                                          limit=max(1, min(limit, MAX_LISTED_ITEMS))))

    # ------------------------------------------------------------------ scheduling

    def is_due(self, source: EventSource, now: datetime) -> bool:
        if not source.enabled:
            return False
        if source.backoff_until is not None and now < source.backoff_until:
            return False
        eligible = self._next_eligible.get(source.id)
        if eligible is not None and now < eligible:
            return False
        return source.last_polled_at is None or now >= source.last_polled_at + timedelta(
            minutes=source.poll_interval_minutes)

    def poll_now(self, owner_id: UUID, source_id: UUID, now: datetime | None = None) -> PollResult:
        source = self.get_source(owner_id, source_id)
        if not source.enabled:
            raise SourceConflict("The source is turned off; turn it on before polling it.")
        return self._poll(source, now or datetime.now(timezone.utc), "manual")

    def poll_due(self, owner_id: UUID, now: datetime | None = None) -> dict[str, int]:
        """Runner hook: start a background cycle for the due sources and return at once (never blocks a tick)."""
        with self._dispatch_lock:
            if self._closing.is_set():
                return {"dispatched": 0, "closing": 1}
            if self._dispatcher is not None and self._dispatcher.is_alive():
                return {"dispatched": 0, "busy": 1}
            self._dispatcher = threading.Thread(target=self._background_cycle, args=(owner_id, now),
                                                name="jarvis-sources-cycle", daemon=True)
            self._dispatcher.start()
            return {"dispatched": 1}

    def _background_cycle(self, owner_id: UUID, now: datetime | None) -> None:
        try:
            self.run_due_polls(owner_id, now)
        except Exception as exc:  # noqa: BLE001 - a failed cycle (for example storage down) is retried next tick
            logger.warning("Source polling cycle failed: %s", type(exc).__name__)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        dispatcher = self._dispatcher
        if dispatcher is not None:
            dispatcher.join(timeout)
            return not dispatcher.is_alive()
        return True

    def close(self, timeout: float = 30.0) -> bool:
        """Stop starting polls and wait (bounded) for the cycle and polls in flight. True when all finished."""
        self._closing.set()
        return self.wait_idle(timeout)

    def run_due_polls(self, owner_id: UUID, now: datetime | None = None) -> dict[str, int]:
        """One cycle: poll due sources (oldest first) on a small pool within the time budget."""
        now = now or datetime.now(timezone.utc)
        summary = {"polled": 0, "failed": 0, "notified": 0, "skipped": 0, "carried_over": 0}
        sources, _ = self.store.list_sources(owner_id, limit=MAX_SOURCES_PER_OWNER, offset=0)
        pending = sorted((source for source in sources if self.is_due(source, now)),
                         key=lambda source: source.last_polled_at or _EPOCH)[: self.max_sources_per_tick]
        started = monotonic()
        with ThreadPoolExecutor(max_workers=max(1, self.max_concurrent_polls),
                                thread_name_prefix="jarvis-source-poll") as pool:
            running: dict[Any, EventSource] = {}
            while pending or running:
                in_budget = monotonic() - started < self.tick_budget_seconds and not self._closing.is_set()
                while pending and in_budget and len(running) < self.max_concurrent_polls:
                    source = pending.pop(0)
                    running[pool.submit(self._poll, source, now, "scheduled")] = source
                if not in_budget and pending:
                    summary["carried_over"] += len(pending)  # still due: picked up by a later tick
                    pending = []
                if not running:
                    break
                done, _ = wait(list(running), timeout=0.5, return_when=FIRST_COMPLETED)
                for future in done:
                    source = running.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001 - isolation: one source's trouble is logged only
                        logger.warning("Source %s could not be polled: %s", source.id, type(exc).__name__)
                        summary["failed"] += 1
                        continue
                    if result.status == "skipped":
                        summary["skipped"] += 1
                        continue
                    summary["polled"] += 1
                    summary["failed"] += result.status == "failed"
                    summary["notified"] += result.notified + result.held
        self.cycles += 1
        self.last_cycle = summary
        return summary

    # ------------------------------------------------------------------ one poll

    def _run_fetch(self, work: Callable[[], Any]) -> Any:
        """Run ``work`` in a daemon thread with the hard timeout; a timed-out thread is tracked until it ends."""
        with self._polling_lock:
            self._abandoned = {thread for thread in self._abandoned if thread.is_alive()}
            if len(self._abandoned) >= self.max_abandoned_fetches:
                raise _Busy("Too many earlier fetches are still stuck; this poll was not started.")
        outcome: dict[str, Any] = {}

        def target() -> None:
            try:
                outcome["value"] = work()
            except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
                outcome["error"] = exc

        thread = threading.Thread(target=target, name="jarvis-source-fetch", daemon=True)
        thread.start()
        thread.join(self.hard_timeout_seconds)
        if thread.is_alive():
            with self._polling_lock:
                self._abandoned.add(thread)
            raise FetchError("timeout", "The source did not respond in time.")
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("value")

    @property
    def abandoned_fetches(self) -> int:
        with self._polling_lock:
            return sum(1 for thread in self._abandoned if thread.is_alive())

    @property
    def polls_running(self) -> int:
        with self._polling_lock:
            return len(self._polling)

    def _fetch(self, source: EventSource) -> tuple[list[SourceItem], bool]:
        if source.kind == "rss":
            entries, truncated = parse_feed_document(self.feed_fetcher(source.config.url or ""))
            return [self._feed_item(source, entry) for entry in entries], truncated
        events, truncated = scan_folder(source.config.path or "", self.folder_roots,
                                        recursive_depth=source.config.recursive_depth)
        return [self._file_item(source, event) for event in events], truncated

    def _feed_item(self, source: EventSource, entry) -> SourceItem:
        if entry.guid:
            key = _key("guid", entry.guid)
        elif entry.link:
            key = _key("link", entry.link)
        else:
            key = _key("title", entry.title, entry.published_at.isoformat() if entry.published_at else "")
        matched = keyword_match(f"{entry.title}\n{entry.summary or ''}", source.include_keywords,
                                source.exclude_keywords)
        return SourceItem(source_id=source.id, owner_id=source.owner_id, item_key=key, title=entry.title,
                          link=entry.link, published_at=entry.published_at, summary=entry.summary, matched=matched,
                          first_seen_at=_EPOCH, last_seen_at=_EPOCH)

    def _file_item(self, source: EventSource, event) -> SourceItem:
        return SourceItem(
            source_id=source.id, owner_id=source.owner_id, item_key=_key("file", event.relative_path),
            title=event.relative_path[:300], link=None,
            published_at=datetime.fromtimestamp(event.mtime_ns / 1e9, tz=timezone.utc),
            summary=f"{event.size} bytes", matched=keyword_match(event.relative_path, source.include_keywords,
                                                                   source.exclude_keywords),
            first_seen_at=_EPOCH, last_seen_at=_EPOCH, version=f"{event.size}:{event.mtime_ns}")

    def _poll(self, source: EventSource, now: datetime, trigger: str) -> PollResult:
        with self._polling_lock:
            if source.id in self._polling:
                return PollResult(source_id=source.id, status="skipped", trigger=trigger,
                                  consecutive_failures=source.consecutive_failures,
                                  error="A poll of this source is already running.")
            self._polling.add(source.id)
        try:
            if self._closing.is_set():
                return PollResult(source_id=source.id, status="skipped", trigger=trigger,
                                  error="JARVIS is shutting down; the source was not polled.")
            try:
                fetched, truncated = self._run_fetch(lambda: self._fetch(source))
            except _Busy as exc:
                return PollResult(source_id=source.id, status="skipped", trigger=trigger,
                                  consecutive_failures=source.consecutive_failures, error=str(exc))
            except Exception as exc:  # noqa: BLE001 - every fetch or parse failure is isolated to this source
                return self._record_failure(source, now, trigger, _clean_error(exc))
            try:
                return self._record_success(source, now, trigger, fetched, truncated)
            except Exception as exc:  # noqa: BLE001 - storing failed after a good fetch: still back off
                logger.warning("Source %s results could not be stored: %s", source.id, type(exc).__name__)
                return self._record_failure(source, now, trigger, "The poll results could not be stored.")
        finally:
            with self._polling_lock:
                self._polling.discard(source.id)

    def _record_failure(self, source: EventSource, now: datetime, trigger: str, error: str) -> PollResult:
        try:
            outcome = self.store.record_failure(source.owner_id, source.id, now=now, error=error,
                                                disable_after=AUTO_DISABLE_AFTER_FAILURES)
        except Exception as exc:  # noqa: BLE001 - even the failure could not be stored: back off in memory
            logger.warning("Source %s failure could not be stored: %s", source.id, type(exc).__name__)
            failures = source.consecutive_failures + 1
            self._next_eligible[source.id] = now + backoff_for(failures, source.poll_interval_minutes)
            return PollResult(source_id=source.id, status="failed", trigger=trigger, consecutive_failures=failures,
                              error=error)
        if outcome is None:
            return PollResult(source_id=source.id, status="skipped", trigger=trigger, error="The source was deleted.")
        self._next_eligible.pop(source.id, None)
        if outcome.turned_off:  # only the update that turned it off tells the owner, so the notice is sent once
            self._notify(source, "high", f"Source turned off: {source.name}"[:300],
                         f"JARVIS stopped polling the {source.kind} source '{source.name}' after "
                         f"{outcome.consecutive_failures} failed polls in a row. Last error: {error} "
                         "Turn it back on when it is fixed.", now)
        self._audit(source.owner_id, "sources.poll", f"sources:{source.id}",
                    {"trigger": trigger, "status": "failed", "consecutive_failures": outcome.consecutive_failures,
                     "disabled": outcome.turned_off}, status="failed")
        return PollResult(source_id=source.id, status="failed", trigger=trigger,
                          consecutive_failures=outcome.consecutive_failures, disabled=outcome.turned_off, error=error)

    def _record_success(self, source: EventSource, now: datetime, trigger: str, fetched: list[SourceItem],
                        truncated: bool) -> PollResult:
        unique: dict[str, SourceItem] = {}
        for item in fetched:
            unique.setdefault(item.item_key, item)
        items = list(unique.values())
        synced: SyncResult | None = self.store.sync_items(source.owner_id, source.id, items, now=now,
                                                          keep=KEEP_ITEMS_PER_SOURCE,
                                                          interval_minutes=source.poll_interval_minutes)
        if synced is None:
            return PollResult(source_id=source.id, status="skipped", trigger=trigger, error="The source was deleted.")
        reported = [(item, "new") for item in synced.new] + [(item, "changed") for item in synced.changed]
        matched = [(item, kind) for item, kind in reported if item.matched]
        counts = {"notified": 0, "held": 0, "suppressed": 0}
        notified_at = None
        if not synced.baseline and matched:
            notification = self._notify(source, source.importance, *self._message(source, matched), now)
            if notification is None:
                counts["suppressed"] = 1
            else:
                counts["held" if notification.delivered_at is None else "notified"] = 1
                notified_at = notification.created_at
        self.store.record_success(source.owner_id, source.id, now=now, notified_at=notified_at)
        self._next_eligible.pop(source.id, None)
        (self.truncated_sources.add if truncated else self.truncated_sources.discard)(source.id)
        result = PollResult(source_id=source.id, status="ok", trigger=trigger, baseline=synced.baseline,
                            fetched=len(items), truncated=truncated, new=len(synced.new),
                            changed=len(synced.changed), pending=synced.pending, matched=len(matched),
                            summarized=len(matched) > 1 and not synced.baseline, **counts)
        self._audit(source.owner_id, "sources.poll", f"sources:{source.id}",
                    {"trigger": trigger, "status": "ok", "baseline": synced.baseline, "fetched": result.fetched,
                     "truncated": truncated, "new": result.new, "changed": result.changed,
                     "pending": result.pending, "matched": result.matched, "notified": result.notified,
                     "held": result.held, "suppressed": result.suppressed})
        return result

    @staticmethod
    def _message(source: EventSource, matched: list[tuple[SourceItem, str]]) -> tuple[str, str]:
        if len(matched) == 1:
            item, kind = matched[0]
            label = "Changed: " if kind == "changed" else ""
            lines = [f"{'Changed' if kind == 'changed' else 'New'} item from the {source.kind} source "
                     f"'{source.name}' (untrusted external text).", item.title]
            if item.link:
                lines.append(item.link)
            if item.summary:
                lines.append(item.summary[:500])
            return f"{source.name}: {label}{item.title}"[:300], "\n".join(lines)
        changed = sum(1 for _, kind in matched if kind == "changed")
        what = "new or changed items" if changed else "new items"
        shown = matched[: source.max_items_per_poll]
        titles = "\n".join(f"- {'Changed: ' if kind == 'changed' else ''}{item.title[:150]}" for item, kind in shown)
        more = "" if len(matched) <= len(shown) else f"\n... and {len(matched) - len(shown)} more."
        return (f"{len(matched)} {what} from {source.name}"[:300],
                f"{len(matched)} {what} from the {source.kind} source '{source.name}' (untrusted external text):\n"
                f"{titles}{more}\nSee all of them under the source's items.")

    def _notify(self, source: EventSource, priority: str, title: str, message: str, now: datetime):
        if self.scheduler_service is None:
            return None
        from jarvis.scheduler.models import NotificationCreateRequest

        try:
            return self.scheduler_service.dispatch_notification(
                source.owner_id, NotificationCreateRequest(category=NOTIFICATION_CATEGORY, title=title,
                                                           message=message, priority=priority),
                now=now, throttle_key=f"source:{source.id}", throttle_last_dispatched=source.last_notified_at)
        except Exception as exc:  # noqa: BLE001 - the items stay recorded and listed; the poll itself succeeded
            logger.warning("Source notification could not be stored: %s", type(exc).__name__)
            return None

    # ------------------------------------------------------------------ status

    def status_counts(self, owner_id: UUID) -> dict[str, int]:
        sources, _ = self.store.list_sources(owner_id, limit=MAX_SOURCES_PER_OWNER, offset=0)
        return {"enabled": sum(1 for item in sources if item.enabled),
                "disabled": sum(1 for item in sources if not item.enabled),
                "failing": sum(1 for item in sources if item.enabled and item.consecutive_failures),
                "truncated": len(self.truncated_sources & {item.id for item in sources}),
                "polls_running": self.polls_running, "abandoned_fetches": self.abandoned_fetches}


__all__ = ["MAX_BACKOFF", "SourceConflict", "SourceInvalid", "SourceNotFound", "SourceStorageError",
           "SourcesService", "backoff_for", "fence_item_for_model", "keyword_match"]
