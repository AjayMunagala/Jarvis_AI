"""Enrollment storage (v4.8): in memory (non-durable) or PostgreSQL (durable, migration 0064).

State changes are single conditional updates, so an enrollment can be
activated at most once, and a revoked enrollment can never become active
again.
"""

import threading
from datetime import datetime
from typing import Protocol
from uuid import UUID

from jarvis.audit.stores import StorageError, _PostgresBase, _iso  # noqa: F401 - StorageError is re-exported
from jarvis.integrations.models import Enrollment

_COLUMNS = "id, owner_id, adapter_id, display_name, secret_ref, state, created_at, activated_at, revoked_at, last_action_at"


class EnrollmentStore(Protocol):
    durable: bool

    def create(self, enrollment: Enrollment) -> None: ...
    def get(self, owner_id: UUID, enrollment_id: str) -> Enrollment | None: ...
    def list(self, owner_id: UUID, *, limit: int) -> tuple[Enrollment, ...]: ...
    def transition(self, owner_id: UUID, enrollment_id: str, *, from_states: tuple[str, ...], to_state: str,
                   now: datetime) -> Enrollment | None: ...
    def touch(self, owner_id: UUID, enrollment_id: str, now: datetime) -> bool: ...


class MemoryEnrollmentStore:
    durable = False

    def __init__(self) -> None:
        self._items: dict[str, Enrollment] = {}
        self._lock = threading.Lock()

    def create(self, enrollment):
        with self._lock:
            self._items[enrollment.enrollment_id] = enrollment

    def get(self, owner_id, enrollment_id):
        item = self._items.get(enrollment_id)
        return item if item is not None and item.owner_id == owner_id else None

    def list(self, owner_id, *, limit):
        with self._lock:
            items = [item for item in self._items.values() if item.owner_id == owner_id]
        return tuple(sorted(items, key=lambda item: item.created_at, reverse=True)[:limit])

    def transition(self, owner_id, enrollment_id, *, from_states, to_state, now):
        with self._lock:
            item = self._items.get(enrollment_id)
            if item is None or item.owner_id != owner_id or item.state not in from_states:
                return None
            stamp = now.isoformat(timespec="milliseconds")
            update = {"state": to_state, ("activated_at" if to_state == "active" else "revoked_at"): stamp}
            updated = item.model_copy(update=update)
            self._items[enrollment_id] = updated
            return updated

    def touch(self, owner_id, enrollment_id, now):
        with self._lock:
            item = self._items.get(enrollment_id)
            if item is None or item.owner_id != owner_id or item.state != "active":
                return False
            self._items[enrollment_id] = item.model_copy(update={"last_action_at": now.isoformat(timespec="milliseconds")})
            return True


class PostgresEnrollmentStore(_PostgresBase):
    @staticmethod
    def _record(row) -> Enrollment:
        return Enrollment(enrollment_id=row[0], owner_id=row[1], adapter_id=row[2], display_name=row[3],
                          secret_ref=row[4], state=row[5], created_at=_iso(row[6]), activated_at=_iso(row[7]),
                          revoked_at=_iso(row[8]), last_action_at=_iso(row[9]), durable=True)

    def create(self, enrollment):
        self._run(lambda execute: execute(
            f"INSERT INTO jarvis_app.integration_enrollments ({_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (enrollment.enrollment_id, enrollment.owner_id, enrollment.adapter_id, enrollment.display_name,
             enrollment.secret_ref, enrollment.state, enrollment.created_at, enrollment.activated_at,
             enrollment.revoked_at, enrollment.last_action_at)))

    def get(self, owner_id, enrollment_id):
        row = self._run(lambda execute: execute(
            f"SELECT {_COLUMNS} FROM jarvis_app.integration_enrollments WHERE id = %s AND owner_id = %s",
            (enrollment_id, owner_id)).fetchone())
        return self._record(row) if row else None

    def list(self, owner_id, *, limit):
        rows = self._run(lambda execute: execute(
            f"""SELECT {_COLUMNS} FROM jarvis_app.integration_enrollments WHERE owner_id = %s
                ORDER BY created_at DESC, id DESC LIMIT %s""", (owner_id, limit)).fetchall())
        return tuple(self._record(row) for row in rows)

    def transition(self, owner_id, enrollment_id, *, from_states, to_state, now):
        column = "activated_at" if to_state == "active" else "revoked_at"
        row = self._run(lambda execute: execute(
            f"""UPDATE jarvis_app.integration_enrollments SET state = %s, {column} = %s
                WHERE id = %s AND owner_id = %s AND state = ANY(%s) RETURNING {_COLUMNS}""",
            (to_state, now, enrollment_id, owner_id, list(from_states))).fetchone())
        return self._record(row) if row else None

    def touch(self, owner_id, enrollment_id, now):
        row = self._run(lambda execute: execute(
            """UPDATE jarvis_app.integration_enrollments SET last_action_at = %s
               WHERE id = %s AND owner_id = %s AND state = 'active' RETURNING id""",
            (now, enrollment_id, owner_id)).fetchone())
        return row is not None
