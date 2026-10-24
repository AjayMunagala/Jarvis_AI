"""Opt-in event sources (v7.3): poll RSS/Atom feeds and watched folders, deduplicate, filter, notify.

Opt-in: nothing is fetched or watched until the owner creates a source. Sources are polled by the scheduler's
background runner (off unless ``JARVIS_SCHEDULER_RUNNER_ENABLED=true``) or when the owner asks
(``POST /sources/{id}/poll``).

Per poll:
- the first successful poll of a source records the existing items as seen without notifying (no flood);
- later polls notify only items not seen before that pass the keyword filters (include any / exclude any,
  case-insensitive); at most ``max_items_per_poll`` notifications, and one summary notification instead when
  there are more;
- notifications go through ``SchedulerService.dispatch_notification`` with category ``"source"`` and the source's
  importance as priority (never ``urgent``), so quiet hours, the held-duplicate check and the anti-fatigue
  throttle apply. "source" is throttled on purpose (unlike reminders and briefings, which the owner asked for):
  a low/normal item notification is dropped within 15 minutes of the previous "source" notification; high
  importance bypasses the throttle. A dropped item is still recorded and listed under ``GET /sources/{id}/items``.

Isolation: every source is fetched in its own thread with a hard timeout and its own error handling; a failing
source never stops another. Failures back off exponentially (capped at 24 h); after 10 failures in a row the
source is turned off and the owner is told once.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import logging
import threading
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

from jarvis.research.parsers import apply_injection_fence
from jarvis.sources.fetchers import (
    FetchError, HttpFeedFetcher, Resolver, check_feed_url, resolve_watched_folder, scan_folder,
)
from jarvis.sources.models import (
    EventSource, EventSourcePage, PollResult, SourceCreateRequest, SourceItem, SourceItemPage, SourceUpdateRequest,
)
from jarvis.sources.parsers import FeedParseError, parse_feed
from jarvis.sources.stores import SourceStorageError, SourceStore

logger = logging.getLogger(__name__)

MAX_SOURCES_PER_OWNER = 50
KEEP_ITEMS_PER_SOURCE = 1000
MAX_LISTED_ITEMS = 50
AUTO_DISABLE_AFTER_FAILURES = 10
MAX_BACKOFF = timedelta(hours=24)
MAX_SOURCES_PER_TICK = 10
HARD_TIMEOUT_SECONDS = 20.0  # the fetcher's own limit is 15 s; this also bounds DNS resolution and folder scans
NOTIFICATION_CATEGORY = "source"


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


def backoff_for(failures: int, interval_minutes: int) -> timedelta:
    """Exponential: twice the interval after one failure, four times after two, ..., at most 24 hours."""
    return min(MAX_BACKOFF, timedelta(minutes=interval_minutes) * (2 ** min(failures, 16)))


def fence_item_for_model(source: EventSource, item: SourceItem) -> str:
    """Item text for a model prompt, inside the untrusted-content fence used for documents."""
    body = "\n".join(part for part in (item.title, item.link or "", item.summary or "") if part)
    return apply_injection_fence(f"source:{source.name}", f"source/{source.kind}", body)


def _clean_error(exc: BaseException) -> str:
    if isinstance(exc, (FetchError, FeedParseError)):
        return str(exc)[:200] or "The source could not be read."
    return f"Unexpected error ({type(exc).__name__})."[:200]


def _run_with_timeout(work: Callable[[], Any], timeout: float) -> Any:
    """Run ``work`` in a daemon thread; give up after ``timeout`` (the abandoned result is discarded)."""
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["value"] = work()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            outcome["error"] = exc

    thread = threading.Thread(target=target, name="jarvis-source-fetch", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise FetchError("timeout", "The source did not respond in time.")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


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
        self._polling: set[UUID] = set()
        self._polling_lock = threading.Lock()

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
        if changes.get("enabled") and not current.enabled:
            # Turning a source back on (for example after it was turned off for failing) starts afresh.
            changes.update(consecutive_failures=0, backoff_until=None)
        if current.kind == "folder" and changes.get("enabled"):
            try:
                resolve_watched_folder(current.config.path or "", self.folder_roots)
            except FetchError as exc:
                raise SourceInvalid(str(exc)) from None
        updated = self.store.update_settings(current.model_copy(update={**changes, "updated_at": now}))
        if updated is None:
            raise SourceNotFound("The source was not found.")
        self._audit(owner_id, "sources.update", f"sources:{source_id}",
                    {"enabled": updated.enabled, "fields": len(req.model_fields_set)})
        return updated

    def delete_source(self, owner_id: UUID, source_id: UUID) -> None:
        if not self.store.delete_source(owner_id, source_id):
            raise SourceNotFound("The source was not found.")
        self._audit(owner_id, "sources.delete", f"sources:{source_id}", {"deleted": True})

    def list_items(self, owner_id: UUID, source_id: UUID, limit: int = MAX_LISTED_ITEMS) -> SourceItemPage:
        self.get_source(owner_id, source_id)
        return SourceItemPage(items=self.store.list_items(owner_id, source_id,
                                                          limit=max(1, min(limit, MAX_LISTED_ITEMS))))

    # ------------------------------------------------------------------ polling

    def is_due(self, source: EventSource, now: datetime) -> bool:
        if not source.enabled:
            return False
        if source.backoff_until is not None and now < source.backoff_until:
            return False
        return source.last_polled_at is None or now >= source.last_polled_at + timedelta(
            minutes=source.poll_interval_minutes)

    def poll_now(self, owner_id: UUID, source_id: UUID, now: datetime | None = None) -> PollResult:
        source = self.get_source(owner_id, source_id)
        if not source.enabled:
            raise SourceConflict("The source is turned off; turn it on before polling it.")
        return self._poll(source, now or datetime.now(timezone.utc), "manual")

    def poll_due(self, owner_id: UUID, now: datetime | None = None) -> dict[str, int]:
        """One runner tick: poll each due source independently; a failing source never stops the others."""
        now = now or datetime.now(timezone.utc)
        summary = {"polled": 0, "failed": 0, "notified": 0}
        sources, _ = self.store.list_sources(owner_id, limit=MAX_SOURCES_PER_OWNER, offset=0)
        due = sorted((source for source in sources if self.is_due(source, now)),
                     key=lambda source: source.last_polled_at or datetime.min.replace(tzinfo=timezone.utc))
        for source in due[: self.max_sources_per_tick]:
            try:
                result = self._poll(source, now, "scheduled")
            except Exception as exc:  # noqa: BLE001 - isolation: storage trouble for one source is logged only
                logger.warning("Source %s could not be polled: %s", source.id, type(exc).__name__)
                summary["failed"] += 1
                continue
            if result.status == "skipped":
                continue
            summary["polled"] += 1
            summary["failed"] += result.status == "failed"
            summary["notified"] += result.notified + result.held
        return summary

    def _fetch(self, source: EventSource) -> list[SourceItem]:
        if source.kind == "rss":
            raw = self.feed_fetcher(source.config.url or "")
            entries = parse_feed(raw)
            return [self._feed_item(source, entry) for entry in entries]
        events = scan_folder(source.config.path or "", self.folder_roots,
                             recursive_depth=source.config.recursive_depth)
        return [self._file_item(source, event) for event in events]

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
        title = event.relative_path[:300]
        return SourceItem(
            source_id=source.id, owner_id=source.owner_id,
            item_key=_key("file", event.relative_path, str(event.size), str(event.mtime_ns)), title=title,
            link=None, published_at=datetime.fromtimestamp(event.mtime_ns / 1e9, tz=timezone.utc),
            summary=f"{event.size} bytes", matched=keyword_match(event.relative_path, source.include_keywords,
                                                                   source.exclude_keywords),
            first_seen_at=_EPOCH, last_seen_at=_EPOCH)

    def _poll(self, source: EventSource, now: datetime, trigger: str) -> PollResult:
        with self._polling_lock:
            if source.id in self._polling:
                return PollResult(source_id=source.id, status="skipped", trigger=trigger,
                                  consecutive_failures=source.consecutive_failures,
                                  error="A poll of this source is already running.")
            self._polling.add(source.id)
        try:
            try:
                fetched = _run_with_timeout(lambda: self._fetch(source), self.hard_timeout_seconds)
            except Exception as exc:  # noqa: BLE001 - every fetch or parse failure is isolated to this source
                return self._record_failure(source, now, trigger, exc)
            return self._record_success(source, now, trigger, fetched)
        finally:
            with self._polling_lock:
                self._polling.discard(source.id)

    def _record_failure(self, source: EventSource, now: datetime, trigger: str, exc: BaseException) -> PollResult:
        failures = source.consecutive_failures + 1
        error = _clean_error(exc)
        disable = failures >= AUTO_DISABLE_AFTER_FAILURES
        stored = self.store.record_state(
            source.owner_id, source.id, last_polled_at=now, last_success_at=source.last_success_at,
            consecutive_failures=failures, last_error=error, backoff_until=now + backoff_for(
                failures, source.poll_interval_minutes), disable=disable, now=now)
        if not stored:
            return PollResult(source_id=source.id, status="skipped", trigger=trigger, error="The source was deleted.")
        if disable:
            self._notify(source.owner_id, "high", f"Source turned off: {source.name}"[:300],
                         f"JARVIS stopped polling the {source.kind} source '{source.name}' after "
                         f"{failures} failed polls in a row. Last error: {error} Turn it back on when it is fixed.",
                         now)
        self._audit(source.owner_id, "sources.poll", f"sources:{source.id}",
                    {"trigger": trigger, "status": "failed", "consecutive_failures": failures, "disabled": disable},
                    status="failed")
        return PollResult(source_id=source.id, status="failed", trigger=trigger, consecutive_failures=failures,
                          disabled=disable, error=error)

    def _record_success(self, source: EventSource, now: datetime, trigger: str,
                        fetched: list[SourceItem]) -> PollResult:
        unique: dict[str, SourceItem] = {}
        for item in fetched:
            unique.setdefault(item.item_key, item.model_copy(update={"first_seen_at": now, "last_seen_at": now}))
        items = list(unique.values())
        inserted = self.store.record_items(source.owner_id, source.id, items, now=now, keep=KEEP_ITEMS_PER_SOURCE)
        if inserted is None:
            return PollResult(source_id=source.id, status="skipped", trigger=trigger, error="The source was deleted.")
        baseline = source.last_success_at is None
        new_items = [item for item in items if item.item_key in inserted]
        matched = [item for item in new_items if item.matched]
        counts = {"notified": 0, "held": 0, "suppressed": 0}
        summarized = False
        if not baseline and matched:
            if len(matched) <= source.max_items_per_poll:
                for item in matched:
                    self._count(counts, self._notify(
                        source.owner_id, source.importance, f"{source.name}: {item.title}"[:300],
                        self._item_message(source, item), now))
            else:
                summarized = True
                titles = "\n".join(f"- {item.title[:150]}" for item in matched[: source.max_items_per_poll])
                self._count(counts, self._notify(
                    source.owner_id, source.importance, f"{len(matched)} new items from {source.name}"[:300],
                    f"{len(matched)} new items from the {source.kind} source '{source.name}' (untrusted external "
                    f"text). The first {source.max_items_per_poll}:\n{titles}\nSee all of them under the source's "
                    "items.", now))
        self.store.record_state(source.owner_id, source.id, last_polled_at=now, last_success_at=now,
                                consecutive_failures=0, last_error=None, backoff_until=None, disable=False, now=now)
        result = PollResult(source_id=source.id, status="ok", trigger=trigger, baseline=baseline, fetched=len(items),
                            new=len(new_items), matched=len(matched), summarized=summarized, **counts)
        self._audit(source.owner_id, "sources.poll", f"sources:{source.id}",
                    {"trigger": trigger, "status": "ok", "baseline": baseline, "fetched": result.fetched,
                     "new": result.new, "matched": result.matched, "notified": result.notified, "held": result.held,
                     "suppressed": result.suppressed, "summarized": summarized})
        return result

    @staticmethod
    def _item_message(source: EventSource, item: SourceItem) -> str:
        lines = [f"New item from the {source.kind} source '{source.name}' (untrusted external text).", item.title]
        if item.link:
            lines.append(item.link)
        if item.summary:
            lines.append(item.summary[:500])
        return "\n".join(lines)

    @staticmethod
    def _count(counts: dict[str, int], notification) -> None:
        if notification is None:
            counts["suppressed"] += 1
        elif notification.delivered_at is None:
            counts["held"] += 1
        else:
            counts["notified"] += 1

    def _notify(self, owner_id: UUID, priority: str, title: str, message: str, now: datetime):
        if self.scheduler_service is None:
            return None
        from jarvis.scheduler.models import NotificationCreateRequest

        try:
            return self.scheduler_service.dispatch_notification(owner_id, NotificationCreateRequest(
                category=NOTIFICATION_CATEGORY, title=title, message=message, priority=priority), now=now)
        except Exception as exc:  # noqa: BLE001 - the items stay recorded and listed; the poll itself succeeded
            logger.warning("Source notification could not be stored: %s", type(exc).__name__)
            return None


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

__all__ = ["SourceConflict", "SourceInvalid", "SourceNotFound", "SourceStorageError", "SourcesService",
           "fence_item_for_model", "keyword_match"]
