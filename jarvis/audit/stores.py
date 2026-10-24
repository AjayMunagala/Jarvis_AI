"""Audit and approval storage: in-memory (non-durable) and PostgreSQL (durable).

PostgreSQL storage is used only when persistence is enabled (decision D7). All
SQL is parameterized and owner-scoped; each operation runs under the existing
transaction-local statement-timeout helpers. Approval decisions are a single
conditional ``UPDATE`` so a request can be decided at most once and never after
it expires. Using an approved request (v4.3) is also a single conditional
``UPDATE``, so an approval can never run its action twice.
"""

import json
import threading
from collections import deque
from datetime import datetime, timezone
from time import monotonic
from typing import Callable, Protocol
from uuid import UUID

from jarvis.audit.models import ApprovalRequest, AuditRecord, execution_for
from jarvis.persistence.operation_timeout import bounded_connect_timeout, execute_with_deadline, operation_deadline


class StorageError(Exception):
    """Sanitized storage failure."""


class AuditStore(Protocol):
    durable: bool

    def append(self, record: AuditRecord) -> None: ...
    def recent(self, owner_id: UUID, *, limit: int, category: str | None = None) -> tuple[AuditRecord, ...]: ...


class ApprovalStore(Protocol):
    durable: bool

    def create(self, request: ApprovalRequest) -> None: ...
    def get(self, owner_id: UUID, approval_id: str) -> ApprovalRequest | None: ...
    def list(self, owner_id: UUID, *, limit: int, state: str | None = None) -> tuple[ApprovalRequest, ...]: ...
    def decide(self, owner_id: UUID, approval_id: str, *, state: str, note: str | None, now: datetime) -> ApprovalRequest | None: ...
    def expire(self, owner_id: UUID, *, now: datetime) -> int: ...
    def consume(self, owner_id: UUID, approval_id: str, *, category: str, action: str, scope_sha256: str,
                now: datetime, decided_after: datetime) -> ApprovalRequest | None: ...


# --------------------------------------------------------------- memory

class MemoryAuditStore:
    durable = False

    def __init__(self, capacity: int = 1000):
        self._events: deque[AuditRecord] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def append(self, record: AuditRecord) -> None:
        with self._lock:
            self._events.append(record)

    def recent(self, owner_id, *, limit, category=None):
        with self._lock:
            items = [item for item in self._events if item.owner_id == owner_id and (category is None or item.category == category)]
        return tuple(items[-limit:][::-1])


class MemoryApprovalStore:
    durable = False

    def __init__(self, capacity: int = 500):
        self._items: dict[str, ApprovalRequest] = {}
        self._capacity = capacity
        self._lock = threading.Lock()

    def create(self, request):
        with self._lock:
            if len(self._items) >= self._capacity:
                oldest = min(self._items.values(), key=lambda item: item.created_at)
                self._items.pop(oldest.approval_id, None)
            self._items[request.approval_id] = request

    def get(self, owner_id, approval_id):
        item = self._items.get(approval_id)
        return item if item is not None and item.owner_id == owner_id else None

    def list(self, owner_id, *, limit, state=None):
        with self._lock:
            items = [item for item in self._items.values() if item.owner_id == owner_id and (state is None or item.state == state)]
        return tuple(sorted(items, key=lambda item: item.created_at, reverse=True)[:limit])

    def decide(self, owner_id, approval_id, *, state, note, now):
        with self._lock:
            item = self._items.get(approval_id)
            if (item is None or item.owner_id != owner_id or item.state != "pending"
                    or datetime.fromisoformat(item.expires_at) <= now):
                return None
            updated = item.model_copy(update={"state": state, "decided_at": now.isoformat(timespec="milliseconds"),
                                              "decision_note": note})
            self._items[approval_id] = updated
            return updated

    def expire(self, owner_id, *, now):
        count = 0
        with self._lock:
            for key, item in list(self._items.items()):
                if item.owner_id == owner_id and item.state == "pending" and datetime.fromisoformat(item.expires_at) <= now:
                    self._items[key] = item.model_copy(update={"state": "expired"})
                    count += 1
        return count

    def consume(self, owner_id, approval_id, *, category, action, scope_sha256, now, decided_after):
        with self._lock:
            item = self._items.get(approval_id)
            if (item is None or item.owner_id != owner_id or item.state != "approved" or item.consumed_at is not None
                    or (item.category, item.action, item.scope_sha256) != (category, action, scope_sha256)
                    or item.decided_at is None or datetime.fromisoformat(item.decided_at) <= decided_after):
                return None
            updated = item.model_copy(update={"consumed_at": now.isoformat(timespec="milliseconds")})
            self._items[approval_id] = updated
            return updated


# ------------------------------------------------------------- postgres

_APPROVAL_COLUMNS = ("id, owner_id, category, action, target, reason, risk, expected_side_effects, scope_sha256, "
                     "correlation_id, created_at, expires_at, state, decided_at, decision_note, consumed_at")


class _PostgresBase:
    durable = True

    def __init__(self, database_url: str, *, connect_timeout_seconds: int = 3, operation_timeout_seconds: int = 5,
                 connect: Callable | None = None):
        if connect is None:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError("PostgreSQL audit storage requires the optional 'persistence' dependency.") from exc
            connect = psycopg.connect
        self._url = database_url
        self._connect = connect
        self._connect_timeout = connect_timeout_seconds
        self._operation_timeout = operation_timeout_seconds

    def _run(self, work):
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        try:
            with self._connect(self._url, connect_timeout=bounded_connect_timeout(
                    self._connect_timeout, deadline, clock=monotonic)) as connection:
                return work(lambda query, params=(): execute_with_deadline(connection, deadline, query, params, clock=monotonic))
        except Exception as exc:
            raise StorageError() from exc


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    return str(value)


class PostgresAuditStore(_PostgresBase):
    def append(self, record):
        self._run(lambda execute: execute(
            """INSERT INTO jarvis_app.audit_events
               (id, owner_id, occurred_at, category, event_type, decision, status, subject, correlation_id, details)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
            (record.event_id, record.owner_id, record.occurred_at, record.category, record.event_type, record.decision,
             record.status, record.subject, record.correlation_id, json.dumps(record.details)),
        ))

    def recent(self, owner_id, *, limit, category=None):
        def work(execute):
            if category is None:
                cursor = execute(
                    """SELECT id, owner_id, occurred_at, category, event_type, decision, status, subject, correlation_id, details
                       FROM jarvis_app.audit_events WHERE owner_id = %s ORDER BY occurred_at DESC, id DESC LIMIT %s""",
                    (owner_id, limit))
            else:
                cursor = execute(
                    """SELECT id, owner_id, occurred_at, category, event_type, decision, status, subject, correlation_id, details
                       FROM jarvis_app.audit_events WHERE owner_id = %s AND category = %s
                       ORDER BY occurred_at DESC, id DESC LIMIT %s""",
                    (owner_id, category, limit))
            return cursor.fetchall()
        rows = self._run(work)
        return tuple(AuditRecord(
            event_id=row[0], owner_id=row[1], occurred_at=_iso(row[2]), category=row[3], event_type=row[4],
            decision=row[5], status=row[6], subject=row[7], correlation_id=row[8],
            details=row[9] if isinstance(row[9], dict) else json.loads(row[9] or "{}"), durable=True,
        ) for row in rows)


class PostgresApprovalStore(_PostgresBase):
    @staticmethod
    def _record(row) -> ApprovalRequest:
        return ApprovalRequest(
            approval_id=row[0], owner_id=row[1], category=row[2], action=row[3], target=row[4], reason=row[5],
            risk=row[6], expected_side_effects=row[7], scope_sha256=row[8], correlation_id=row[9],
            created_at=_iso(row[10]), expires_at=_iso(row[11]), state=row[12], decided_at=_iso(row[13]),
            decision_note=row[14], consumed_at=_iso(row[15]), execution=execution_for(row[2]), durable=True,
        )

    def create(self, request):
        self._run(lambda execute: execute(
            f"""INSERT INTO jarvis_app.approval_requests ({_APPROVAL_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (request.approval_id, request.owner_id, request.category, request.action, request.target, request.reason,
             request.risk, request.expected_side_effects, request.scope_sha256, request.correlation_id,
             request.created_at, request.expires_at, request.state, request.decided_at, request.decision_note,
             request.consumed_at),
        ))

    def get(self, owner_id, approval_id):
        row = self._run(lambda execute: execute(
            f"SELECT {_APPROVAL_COLUMNS} FROM jarvis_app.approval_requests WHERE id = %s AND owner_id = %s",
            (approval_id, owner_id)).fetchone())
        return self._record(row) if row else None

    def list(self, owner_id, *, limit, state=None):
        def work(execute):
            if state is None:
                return execute(f"""SELECT {_APPROVAL_COLUMNS} FROM jarvis_app.approval_requests
                                   WHERE owner_id = %s ORDER BY created_at DESC, id DESC LIMIT %s""",
                               (owner_id, limit)).fetchall()
            return execute(f"""SELECT {_APPROVAL_COLUMNS} FROM jarvis_app.approval_requests
                               WHERE owner_id = %s AND state = %s ORDER BY created_at DESC, id DESC LIMIT %s""",
                           (owner_id, state, limit)).fetchall()
        return tuple(self._record(row) for row in self._run(work))

    def decide(self, owner_id, approval_id, *, state, note, now):
        row = self._run(lambda execute: execute(
            f"""UPDATE jarvis_app.approval_requests
                SET state = %s, decided_at = %s, decision_note = %s
                WHERE id = %s AND owner_id = %s AND state = 'pending' AND expires_at > %s
                RETURNING {_APPROVAL_COLUMNS}""",
            (state, now, note, approval_id, owner_id, now)).fetchone())
        return self._record(row) if row else None

    def expire(self, owner_id, *, now):
        rows = self._run(lambda execute: execute(
            """UPDATE jarvis_app.approval_requests SET state = 'expired'
               WHERE owner_id = %s AND state = 'pending' AND expires_at <= %s RETURNING id""",
            (owner_id, now)).fetchall())
        return len(rows)

    def consume(self, owner_id, approval_id, *, category, action, scope_sha256, now, decided_after):
        row = self._run(lambda execute: execute(
            f"""UPDATE jarvis_app.approval_requests SET consumed_at = %s
                WHERE id = %s AND owner_id = %s AND state = 'approved' AND consumed_at IS NULL
                  AND category = %s AND action = %s AND scope_sha256 = %s AND decided_at > %s
                RETURNING {_APPROVAL_COLUMNS}""",
            (now, approval_id, owner_id, category, action, scope_sha256, decided_after)).fetchone())
        return self._record(row) if row else None
