"""Model usage and cost tracking (v6.1).

Every call to a real model provider is recorded as one usage event: the
provider and model, primary or fallback, the outcome, the input and output
token counts the provider reported (``None`` when it reported none), the
duration, and the request's correlation ID. Prompt and reply text are never
recorded.

Recording never waits: ``record`` puts the event on a bounded in-memory queue
and returns at once. One background writer thread drains the queue into the
store (reusing one database connection). If storage is slow or down, the
queue fills and further events are dropped and counted; a chat request is
never slowed down or failed by usage tracking.

Cost is only estimated when the operator declares a price per million tokens
for a model, with its source; otherwise the summary says the cost is unknown
instead of guessing. Days are UTC calendar days. Usage is tracked per
installation, which has one configured owner.
"""

from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
import logging
import queue
import secrets
import threading
from collections import deque
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from jarvis.core.correlation import get_correlation_id

logger = logging.getLogger(__name__)
Outcome = Literal["succeeded", "failed", "timeout", "cancelled"]
QUEUE_CAPACITY = 1000


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class UsageEvent(Frozen):
    event_id: StrictStr = Field(pattern=r"^UE-[a-f0-9]{16}$")
    occurred_at: datetime
    provider: StrictStr = Field(min_length=1, max_length=40)
    model: StrictStr = Field(min_length=1, max_length=200)
    role: Literal["primary", "fallback"] = "primary"
    outcome: Outcome
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    duration_ms: float = Field(ge=0)
    correlation_id: StrictStr | None = Field(default=None, max_length=64)


class ModelUsage(Frozen):
    model: StrictStr
    day: date
    calls: int
    failed_calls: int
    fallback_calls: int
    prompt_tokens: int
    completion_tokens: int
    calls_without_token_counts: int
    estimated_cost_usd: float | None
    cost_note: StrictStr


class UsageTotals(Frozen):
    calls: int
    failed_calls: int
    fallback_calls: int
    prompt_tokens: int
    completion_tokens: int
    calls_without_token_counts: int
    estimated_cost_usd: float | None


class UsageSummary(Frozen):
    days: int
    first_day: date
    durable_storage: bool
    totals: UsageTotals
    by_model_and_day: tuple[ModelUsage, ...]
    recording_failures: int
    dropped_events: int
    note: StrictStr


@dataclass(frozen=True)
class Price:
    input_per_million: float
    output_per_million: float
    source: str


@dataclass(frozen=True)
class _Row:
    model: str
    day: date
    calls: int
    failed: int
    fallback: int
    prompt: int
    completion: int
    missing: int


def _aggregate(events) -> list[_Row]:
    groups: dict[tuple[str, date], list[UsageEvent]] = {}
    for event in events:
        groups.setdefault((event.model, event.occurred_at.astimezone(timezone.utc).date()), []).append(event)
    return [_Row(model, day, len(items), sum(1 for e in items if e.outcome != "succeeded"),
                 sum(1 for e in items if e.role == "fallback"), sum(e.prompt_tokens or 0 for e in items),
                 sum(e.completion_tokens or 0 for e in items),
                 sum(1 for e in items if e.prompt_tokens is None or e.completion_tokens is None))
            for (model, day), items in groups.items()]


class MemoryUsageStore:
    durable = False

    def __init__(self, capacity: int = 10_000):
        self._events: deque[UsageEvent] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self.evicted = 0

    def append(self, event: UsageEvent) -> None:
        with self._lock:
            if len(self._events) == self._events.maxlen:
                self.evicted += 1
            self._events.append(event)

    def aggregate(self, start: datetime) -> list[_Row]:
        with self._lock:
            return _aggregate([event for event in self._events if event.occurred_at >= start])

    def events(self) -> list[UsageEvent]:
        with self._lock:
            return list(self._events)


class PostgresUsageStore:
    """Writes come only from the usage writer thread, which keeps one connection open (reopened after an error)."""

    durable = True

    def __init__(self, database_url: str, *, connect_timeout_seconds: int = 3, operation_timeout_seconds: int = 5):
        self.database_url = database_url
        self.connect_timeout_seconds = connect_timeout_seconds
        self.operation_timeout_seconds = operation_timeout_seconds
        self.evicted = 0
        self._writer_connection = None

    def _connect(self):
        import psycopg

        return psycopg.connect(self.database_url, connect_timeout=int(self.connect_timeout_seconds), autocommit=True,
                               options=f"-c statement_timeout={int(self.operation_timeout_seconds * 1000)}")

    def append(self, event: UsageEvent) -> None:
        try:
            if self._writer_connection is None or self._writer_connection.closed:
                self._writer_connection = self._connect()
            self._writer_connection.execute(
                """INSERT INTO jarvis_app.usage_events (id, occurred_at, provider, model, role, outcome, prompt_tokens,
                       completion_tokens, duration_ms, correlation_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                (event.event_id, event.occurred_at, event.provider, event.model, event.role, event.outcome,
                 event.prompt_tokens, event.completion_tokens, event.duration_ms, event.correlation_id))
        except Exception:
            if self._writer_connection is not None:
                try:
                    self._writer_connection.close()
                except Exception:  # noqa: BLE001
                    pass
            self._writer_connection = None
            raise

    def aggregate(self, start: datetime) -> list[_Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT model, (occurred_at AT TIME ZONE 'UTC')::date AS day, count(*),
                          count(*) FILTER (WHERE outcome <> 'succeeded'), count(*) FILTER (WHERE role = 'fallback'),
                          coalesce(sum(prompt_tokens), 0), coalesce(sum(completion_tokens), 0),
                          count(*) FILTER (WHERE prompt_tokens IS NULL OR completion_tokens IS NULL)
                   FROM jarvis_app.usage_events WHERE occurred_at >= %s GROUP BY 1, 2""", (start,)).fetchall()
        return [_Row(r[0], r[1], r[2], r[3], r[4], int(r[5]), int(r[6]), r[7]) for r in rows]


class UsageService:
    def __init__(self, store, prices: dict[str, Price] | None = None, *, capacity: int = QUEUE_CAPACITY):
        self.store = store
        self.prices = dict(prices or {})
        self.recording_failures = 0
        self.dropped_events = 0
        self._queue: queue.Queue[UsageEvent | None] = queue.Queue(maxsize=capacity)
        self._writer = threading.Thread(target=self._drain, name="jarvis-usage-writer", daemon=True)
        self._writer.start()

    # ------------------------------------------------------------ recording (never blocks)

    def record(self, *, provider: str, model: str, outcome: Outcome, duration_ms: float,
               prompt_tokens: int | None = None, completion_tokens: int | None = None,
               role: str = "primary") -> None:
        try:
            event = UsageEvent(event_id="UE-" + secrets.token_hex(8), occurred_at=datetime.now(timezone.utc),
                               provider=provider[:40], model=model[:200], role=role, outcome=outcome,
                               prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                               duration_ms=round(max(0.0, duration_ms), 3), correlation_id=get_correlation_id())
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped_events += 1
        except Exception as exc:  # noqa: BLE001 - usage tracking must never break a request
            self.recording_failures += 1
            logger.warning("Model usage could not be recorded: %s", type(exc).__name__)

    def _drain(self) -> None:
        while True:
            event = self._queue.get()
            try:
                if event is None:
                    return
                self.store.append(event)
            except Exception as exc:  # noqa: BLE001
                self.recording_failures += 1
                logger.warning("Model usage could not be stored: %s", type(exc).__name__)
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait (bounded) until queued events are stored; True when the queue drained in time."""
        deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)
        while self._queue.unfinished_tasks and datetime.now(timezone.utc) < deadline:
            threading.Event().wait(0.01)
        return not self._queue.unfinished_tasks

    def close(self, timeout: float = 2.0) -> None:
        self.flush(timeout)
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    # ------------------------------------------------------------ summary

    def summary(self, days: int = 7) -> UsageSummary:
        days = max(1, min(days, 90))
        self.flush(1.0)
        today = datetime.now(timezone.utc).date()
        first_day = today - timedelta(days=days - 1)
        start = datetime.combine(first_day, clock_time.min, tzinfo=timezone.utc)  # whole UTC days only
        rows = []
        for row in sorted(self.store.aggregate(start), key=lambda item: (item.day, item.model)):
            price = self.prices.get(row.model)
            if price is None:
                cost, note = None, "unknown: no price declared for this model"
            else:
                cost = round(row.prompt / 1e6 * price.input_per_million
                             + row.completion / 1e6 * price.output_per_million, 6)
                note = f"estimate from the declared price ({price.source})"
                if row.missing:
                    note += f"; {row.missing} call(s) without full token counts are not included"
            rows.append(ModelUsage(model=row.model, day=row.day, calls=row.calls, failed_calls=row.failed,
                                   fallback_calls=row.fallback, prompt_tokens=row.prompt,
                                   completion_tokens=row.completion, calls_without_token_counts=row.missing,
                                   estimated_cost_usd=cost, cost_note=note))
        costs = [row.estimated_cost_usd for row in rows]
        totals = UsageTotals(
            calls=sum(row.calls for row in rows), failed_calls=sum(row.failed_calls for row in rows),
            fallback_calls=sum(row.fallback_calls for row in rows),
            prompt_tokens=sum(row.prompt_tokens for row in rows),
            completion_tokens=sum(row.completion_tokens for row in rows),
            calls_without_token_counts=sum(row.calls_without_token_counts for row in rows),
            estimated_cost_usd=(round(sum(costs), 6) if all(cost is not None for cost in costs) else None),
        )
        evicted = getattr(self.store, "evicted", 0)
        note = ("Token counts are as reported by the provider; days are UTC. A total cost is shown only when every "
                "model used has a declared price. Failed or timed-out calls may still be billed by the provider.")
        if evicted:
            note += f" {evicted} older event(s) were evicted from in-memory storage."
        return UsageSummary(days=days, first_day=first_day, durable_storage=bool(getattr(self.store, "durable", False)),
                            totals=totals, by_model_and_day=tuple(rows), recording_failures=self.recording_failures,
                            dropped_events=self.dropped_events, note=note)
