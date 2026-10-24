"""Model usage and cost tracking (v6.1).

Every call to a real model provider is recorded as one usage event: the
provider and model, the input and output token counts the provider reported
(``None`` when it reported none), the outcome and the duration, linked to the
request's correlation ID. Prompt and reply text are never recorded.

Cost is only estimated when the operator declares a price per million tokens
for a model (``JARVIS_OPENAI_PRICE_*`` settings, with a source); otherwise the
summary says the cost is unknown instead of guessing. Usage is tracked per
installation, which has one configured owner.

Recording is best effort: a storage failure is logged and counted, and never
fails or slows down the chat request (the write runs in a worker thread).
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import logging
import secrets
import threading
from collections import deque
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from jarvis.core.correlation import get_correlation_id

logger = logging.getLogger(__name__)
Outcome = Literal["succeeded", "failed", "timeout"]


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


class UsageSummary(Frozen):
    days: int
    durable_storage: bool
    totals: dict[str, float | int | None]
    by_model_and_day: tuple[ModelUsage, ...]
    recording_failures: int
    note: StrictStr


@dataclass(frozen=True)
class Price:
    input_per_million: float
    output_per_million: float
    source: str


class MemoryUsageStore:
    durable = False

    def __init__(self, capacity: int = 10_000):
        self._events: deque[UsageEvent] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def append(self, event: UsageEvent) -> None:
        with self._lock:
            self._events.append(event)

    def since(self, start: datetime) -> list[UsageEvent]:
        with self._lock:
            return [event for event in self._events if event.occurred_at >= start]


class PostgresUsageStore:
    durable = True

    def __init__(self, database_url: str, *, connect_timeout_seconds: int = 3, operation_timeout_seconds: int = 5):
        self.database_url = database_url
        self.connect_timeout_seconds = connect_timeout_seconds
        self.operation_timeout_seconds = operation_timeout_seconds

    def _connect(self):
        import psycopg

        return psycopg.connect(self.database_url, connect_timeout=int(self.connect_timeout_seconds),
                               options=f"-c statement_timeout={int(self.operation_timeout_seconds * 1000)}")

    def append(self, event: UsageEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO jarvis_app.usage_events (id, occurred_at, provider, model, role, outcome, prompt_tokens,
                       completion_tokens, duration_ms, correlation_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (event.event_id, event.occurred_at, event.provider, event.model, event.role, event.outcome,
                 event.prompt_tokens, event.completion_tokens, event.duration_ms, event.correlation_id))

    def since(self, start: datetime) -> list[UsageEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id, occurred_at, provider, model, role, outcome, prompt_tokens, completion_tokens,
                          duration_ms, correlation_id
                   FROM jarvis_app.usage_events WHERE occurred_at >= %s ORDER BY occurred_at""", (start,)).fetchall()
        return [UsageEvent(event_id=r[0], occurred_at=r[1], provider=r[2], model=r[3], role=r[4], outcome=r[5],
                           prompt_tokens=r[6], completion_tokens=r[7], duration_ms=float(r[8]), correlation_id=r[9])
                for r in rows]


class UsageService:
    def __init__(self, store, prices: dict[str, Price] | None = None):
        self.store = store
        self.prices = dict(prices or {})
        self.recording_failures = 0

    def record(self, *, provider: str, model: str, outcome: Outcome, duration_ms: float,
               prompt_tokens: int | None = None, completion_tokens: int | None = None,
               role: str = "primary") -> None:
        """Best effort: never raises."""
        try:
            event = UsageEvent(event_id="UE-" + secrets.token_hex(8), occurred_at=datetime.now(timezone.utc),
                               provider=provider[:40], model=model[:200], role=role, outcome=outcome,
                               prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                               duration_ms=round(max(0.0, duration_ms), 3), correlation_id=get_correlation_id())
            self.store.append(event)
        except Exception as exc:  # noqa: BLE001 - usage tracking must never break a request
            self.recording_failures += 1
            logger.warning("Model usage could not be recorded: %s", type(exc).__name__)

    def summary(self, days: int = 7) -> UsageSummary:
        days = max(1, min(days, 90))
        start = datetime.now(timezone.utc) - timedelta(days=days)
        groups: dict[tuple[str, date], list[UsageEvent]] = {}
        for event in self.store.since(start):
            groups.setdefault((event.model, event.occurred_at.astimezone(timezone.utc).date()), []).append(event)
        rows = []
        for (model, day), events in sorted(groups.items(), key=lambda item: (item[0][1], item[0][0])):
            prompt = sum(event.prompt_tokens or 0 for event in events)
            completion = sum(event.completion_tokens or 0 for event in events)
            missing = sum(1 for event in events if event.prompt_tokens is None and event.outcome == "succeeded")
            price = self.prices.get(model)
            if price is None:
                cost, note = None, "unknown: no price declared for this model"
            else:
                cost = round(prompt / 1e6 * price.input_per_million + completion / 1e6 * price.output_per_million, 6)
                note = f"estimate from the declared price ({price.source})"
                if missing:
                    note += f"; {missing} call(s) had no token counts and are not included"
            rows.append(ModelUsage(model=model, day=day, calls=len(events),
                                   failed_calls=sum(1 for event in events if event.outcome != "succeeded"),
                                   fallback_calls=sum(1 for event in events if event.role == "fallback"),
                                   prompt_tokens=prompt, completion_tokens=completion,
                                   calls_without_token_counts=missing, estimated_cost_usd=cost, cost_note=note))
        costs = [row.estimated_cost_usd for row in rows]
        totals = {
            "calls": sum(row.calls for row in rows),
            "failed_calls": sum(row.failed_calls for row in rows),
            "prompt_tokens": sum(row.prompt_tokens for row in rows),
            "completion_tokens": sum(row.completion_tokens for row in rows),
            "estimated_cost_usd": (round(sum(costs), 6) if rows and all(cost is not None for cost in costs)
                                   else None),
        }
        return UsageSummary(days=days, durable_storage=bool(getattr(self.store, "durable", False)), totals=totals,
                            by_model_and_day=tuple(rows), recording_failures=self.recording_failures,
                            note="Token counts are as reported by the provider. A total cost is shown only when "
                                 "every model used has a declared price.")
