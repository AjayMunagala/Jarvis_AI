"""Travel storage: in-memory (non-durable) and PostgreSQL (durable) (v7.4).

Caps are checked in the same lock (memory) or transaction (PostgreSQL: a per-owner advisory lock for decisions
and trips, the trip row lock for segments) as the insert, so concurrent creates cannot pass a cap.
PostgreSQL queries are parameterized, owner-scoped and run under a bounded operation deadline.
"""

import json
from time import monotonic
import threading
from typing import Any, Callable, Literal, Protocol
from uuid import UUID

from jarvis.persistence.operation_timeout import (
    bounded_connect_timeout, execute_with_deadline, operation_deadline,
)
from jarvis.travel.models import Decision, ReminderLink, Segment, Trip


class TravelStorageError(Exception):
    """Sanitized storage failure for travel operations."""


class TravelStore(Protocol):
    durable: bool

    def save_decision(self, decision: Decision, *, max_decisions: int) -> bool: ...
    def get_decision(self, owner_id: UUID, decision_id: UUID) -> Decision | None: ...
    def list_decisions(self, owner_id: UUID, *, limit: int, offset: int) -> tuple[list[Decision], int]: ...
    def update_decision(self, decision: Decision) -> bool: ...
    def delete_decision(self, owner_id: UUID, decision_id: UUID) -> bool: ...
    def save_trip(self, trip: Trip, *, max_trips: int) -> bool: ...
    def get_trip(self, owner_id: UUID, trip_id: UUID) -> Trip | None: ...
    def list_trips(self, owner_id: UUID, *, limit: int, offset: int) -> tuple[list[Trip], int]: ...
    def update_trip(self, trip: Trip) -> bool: ...
    def delete_trip(self, owner_id: UUID, trip_id: UUID) -> bool: ...
    def save_segment(self, segment: Segment, *, max_segments: int) -> Literal["saved", "missing", "full"]: ...
    def get_segment(self, owner_id: UUID, trip_id: UUID, segment_id: UUID) -> Segment | None: ...
    def list_segments(self, owner_id: UUID, trip_id: UUID) -> list[Segment]: ...
    def update_segment(self, segment: Segment) -> bool: ...
    def delete_segment(self, owner_id: UUID, trip_id: UUID, segment_id: UUID) -> bool: ...
    def list_links(self, owner_id: UUID, trip_id: UUID) -> list[ReminderLink]: ...
    def get_link(self, owner_id: UUID, segment_id: UUID) -> ReminderLink | None: ...
    def save_link(self, link: ReminderLink) -> bool: ...
    def update_link(self, link: ReminderLink) -> bool: ...
    def delete_link(self, owner_id: UUID, segment_id: UUID) -> bool: ...


# ====================================================================== memory


class MemoryTravelStore:
    """Thread-safe, non-durable store."""

    durable = False

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._decisions: dict[UUID, Decision] = {}
        self._trips: dict[UUID, Trip] = {}
        self._segments: dict[UUID, Segment] = {}
        self._links: dict[UUID, ReminderLink] = {}

    # ---------------------------------------------------------------- decisions

    def save_decision(self, decision: Decision, *, max_decisions: int) -> bool:
        with self._lock:
            if sum(1 for item in self._decisions.values() if item.owner_id == decision.owner_id) >= max_decisions:
                return False
            self._decisions[decision.id] = decision
            return True

    def get_decision(self, owner_id, decision_id):
        with self._lock:
            item = self._decisions.get(decision_id)
            return item if item is not None and item.owner_id == owner_id else None

    def list_decisions(self, owner_id, *, limit, offset):
        with self._lock:
            items = [item for item in self._decisions.values() if item.owner_id == owner_id]
        items.sort(key=lambda item: (item.created_at, str(item.id)), reverse=True)
        return items[offset:offset + limit], len(items)

    def update_decision(self, decision: Decision) -> bool:
        with self._lock:
            current = self._decisions.get(decision.id)
            if current is None or current.owner_id != decision.owner_id:
                return False
            self._decisions[decision.id] = decision
            return True

    def delete_decision(self, owner_id, decision_id) -> bool:
        with self._lock:
            item = self._decisions.get(decision_id)
            if item is None or item.owner_id != owner_id:
                return False
            del self._decisions[decision_id]
            return True

    # ---------------------------------------------------------------- trips

    def save_trip(self, trip: Trip, *, max_trips: int) -> bool:
        with self._lock:
            if sum(1 for item in self._trips.values() if item.owner_id == trip.owner_id) >= max_trips:
                return False
            self._trips[trip.id] = trip
            return True

    def get_trip(self, owner_id, trip_id):
        with self._lock:
            item = self._trips.get(trip_id)
            return item if item is not None and item.owner_id == owner_id else None

    def list_trips(self, owner_id, *, limit, offset):
        with self._lock:
            items = [item for item in self._trips.values() if item.owner_id == owner_id]
        items.sort(key=lambda item: (item.start_date, item.created_at, str(item.id)), reverse=True)
        return items[offset:offset + limit], len(items)

    def update_trip(self, trip: Trip) -> bool:
        with self._lock:
            current = self._trips.get(trip.id)
            if current is None or current.owner_id != trip.owner_id:
                return False
            self._trips[trip.id] = trip
            return True

    def delete_trip(self, owner_id, trip_id) -> bool:
        with self._lock:
            item = self._trips.get(trip_id)
            if item is None or item.owner_id != owner_id:
                return False
            del self._trips[trip_id]
            for key in [key for key, seg in self._segments.items() if seg.trip_id == trip_id]:
                del self._segments[key]
            for key in [key for key, link in self._links.items() if link.trip_id == trip_id]:
                del self._links[key]
            return True

    # ---------------------------------------------------------------- segments

    def save_segment(self, segment: Segment, *, max_segments: int):
        with self._lock:
            trip = self._trips.get(segment.trip_id)
            if trip is None or trip.owner_id != segment.owner_id:
                return "missing"
            if sum(1 for item in self._segments.values() if item.trip_id == segment.trip_id) >= max_segments:
                return "full"
            self._segments[segment.id] = segment
            return "saved"

    def get_segment(self, owner_id, trip_id, segment_id):
        with self._lock:
            item = self._segments.get(segment_id)
            return item if item is not None and item.owner_id == owner_id and item.trip_id == trip_id else None

    def list_segments(self, owner_id, trip_id):
        with self._lock:
            items = [item for item in self._segments.values() if item.owner_id == owner_id and item.trip_id == trip_id]
        items.sort(key=lambda item: (item.start_utc, item.end_utc, str(item.id)))
        return items

    def update_segment(self, segment: Segment) -> bool:
        with self._lock:
            current = self._segments.get(segment.id)
            if current is None or current.owner_id != segment.owner_id or current.trip_id != segment.trip_id:
                return False
            self._segments[segment.id] = segment
            return True

    def delete_segment(self, owner_id, trip_id, segment_id) -> bool:
        with self._lock:
            item = self._segments.get(segment_id)
            if item is None or item.owner_id != owner_id or item.trip_id != trip_id:
                return False
            del self._segments[segment_id]
            self._links.pop(segment_id, None)
            return True

    # ---------------------------------------------------------------- reminder links

    def list_links(self, owner_id, trip_id):
        with self._lock:
            return [link for link in self._links.values() if link.owner_id == owner_id and link.trip_id == trip_id]

    def get_link(self, owner_id, segment_id):
        with self._lock:
            link = self._links.get(segment_id)
            return link if link is not None and link.owner_id == owner_id else None

    def save_link(self, link: ReminderLink) -> bool:
        with self._lock:
            segment = self._segments.get(link.segment_id)
            if link.segment_id in self._links or segment is None or segment.owner_id != link.owner_id \
                    or segment.trip_id != link.trip_id:
                return False
            self._links[link.segment_id] = link
            return True

    def update_link(self, link: ReminderLink) -> bool:
        with self._lock:
            current = self._links.get(link.segment_id)
            if current is None or current.owner_id != link.owner_id:
                return False
            self._links[link.segment_id] = link
            return True

    def delete_link(self, owner_id, segment_id) -> bool:
        with self._lock:
            link = self._links.get(segment_id)
            if link is None or link.owner_id != owner_id:
                return False
            del self._links[segment_id]
            return True


# ====================================================================== PostgreSQL

_DECISION_COLUMNS = "id, owner_id, question, options, criteria, notes, created_at, updated_at"
_TRIP_COLUMNS = ("id, owner_id, name, travellers, currency, budget, start_date, end_date, home_timezone, daily_food, "
                 "daily_local_transport, daily_other, min_connection_minutes_flight, min_connection_minutes_other, "
                 "notes, created_at, updated_at")
_SEGMENT_COLUMNS = ("id, owner_id, trip_id, kind, title, from_place, to_place, start_local, start_timezone, "
                    "end_local, end_timezone, start_utc, end_utc, reference, cost_amount, cost_currency, notes, "
                    "created_at, updated_at")
_LINK_COLUMNS = "owner_id, trip_id, segment_id, reminder_id, lead_minutes, remind_at, created_at, updated_at"


def _decision(row) -> Decision:
    return Decision(id=row[0], owner_id=row[1], question=row[2], options=row[3], criteria=row[4], notes=row[5],
                    created_at=row[6], updated_at=row[7])


def _trip(row) -> Trip:
    return Trip(**dict(zip([name.strip() for name in _TRIP_COLUMNS.split(",")], row)))


def _segment(row) -> Segment:
    return Segment(**dict(zip([name.strip() for name in _SEGMENT_COLUMNS.split(",")], row)))


def _link(row) -> ReminderLink:
    return ReminderLink(**dict(zip([name.strip() for name in _LINK_COLUMNS.split(",")], row)))


def _jsonb(items) -> str:
    return json.dumps([item.model_dump(mode="json") for item in items])


def _trip_params(trip: Trip) -> tuple:
    return (trip.id, trip.owner_id, trip.name, trip.travellers, trip.currency, trip.budget, trip.start_date,
            trip.end_date, trip.home_timezone, trip.daily_food, trip.daily_local_transport, trip.daily_other,
            trip.min_connection_minutes_flight, trip.min_connection_minutes_other, trip.notes, trip.created_at,
            trip.updated_at)


def _segment_params(s: Segment) -> tuple:
    return (s.id, s.owner_id, s.trip_id, s.kind, s.title, s.from_place, s.to_place, s.start_local, s.start_timezone,
            s.end_local, s.end_timezone, s.start_utc, s.end_utc, s.reference, s.cost_amount, s.cost_currency,
            s.notes, s.created_at, s.updated_at)


def _placeholders(columns: str) -> str:
    return ", ".join(["%s"] * len(columns.split(",")))


class PostgresTravelStore:
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
        except Exception as exc:  # noqa: BLE001 - sanitized: no driver message, SQL, URL or value is exposed
            raise TravelStorageError(failure) from exc

    # ---------------------------------------------------------------- decisions

    def save_decision(self, decision: Decision, *, max_decisions: int) -> bool:
        def work(execute):
            execute("SELECT pg_advisory_xact_lock(hashtextextended('jarvis.travel_decisions:' || %s::text, 0))",
                    (decision.owner_id,))
            if execute("SELECT count(*) FROM jarvis_app.travel_decisions WHERE owner_id = %s",
                       (decision.owner_id,)).fetchone()[0] >= max_decisions:
                return False
            execute(f"INSERT INTO jarvis_app.travel_decisions ({_DECISION_COLUMNS}) "
                    "VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)",
                    (decision.id, decision.owner_id, decision.question, _jsonb(decision.options),
                     _jsonb(decision.criteria), decision.notes, decision.created_at, decision.updated_at))
            return True

        return self._run(work, "Failed to store the decision.")

    def get_decision(self, owner_id, decision_id):
        row = self._run(lambda execute: execute(
            f"SELECT {_DECISION_COLUMNS} FROM jarvis_app.travel_decisions WHERE id = %s AND owner_id = %s",
            (decision_id, owner_id)).fetchone(), "Failed to read the decision.")
        return _decision(row) if row else None

    def list_decisions(self, owner_id, *, limit, offset):
        def work(execute):
            total = execute("SELECT count(*) FROM jarvis_app.travel_decisions WHERE owner_id = %s",
                            (owner_id,)).fetchone()[0]
            rows = execute(f"SELECT {_DECISION_COLUMNS} FROM jarvis_app.travel_decisions WHERE owner_id = %s "
                           "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s", (owner_id, limit, offset)).fetchall()
            return [_decision(row) for row in rows], int(total)

        return self._run(work, "Failed to list decisions.")

    def update_decision(self, decision: Decision) -> bool:
        return self._run(lambda execute: execute(
            """UPDATE jarvis_app.travel_decisions SET question = %s, options = %s::jsonb, criteria = %s::jsonb,
                   notes = %s, updated_at = %s WHERE id = %s AND owner_id = %s""",
            (decision.question, _jsonb(decision.options), _jsonb(decision.criteria), decision.notes,
             decision.updated_at, decision.id, decision.owner_id)).rowcount == 1, "Failed to update the decision.")

    def delete_decision(self, owner_id, decision_id) -> bool:
        return self._run(lambda execute: execute(
            "DELETE FROM jarvis_app.travel_decisions WHERE id = %s AND owner_id = %s",
            (decision_id, owner_id)).rowcount == 1, "Failed to delete the decision.")

    # ---------------------------------------------------------------- trips

    def save_trip(self, trip: Trip, *, max_trips: int) -> bool:
        def work(execute):
            execute("SELECT pg_advisory_xact_lock(hashtextextended('jarvis.travel_trips:' || %s::text, 0))",
                    (trip.owner_id,))
            if execute("SELECT count(*) FROM jarvis_app.travel_trips WHERE owner_id = %s",
                       (trip.owner_id,)).fetchone()[0] >= max_trips:
                return False
            execute(f"INSERT INTO jarvis_app.travel_trips ({_TRIP_COLUMNS}) VALUES ({_placeholders(_TRIP_COLUMNS)})",
                    _trip_params(trip))
            return True

        return self._run(work, "Failed to store the trip.")

    def get_trip(self, owner_id, trip_id):
        row = self._run(lambda execute: execute(
            f"SELECT {_TRIP_COLUMNS} FROM jarvis_app.travel_trips WHERE id = %s AND owner_id = %s",
            (trip_id, owner_id)).fetchone(), "Failed to read the trip.")
        return _trip(row) if row else None

    def list_trips(self, owner_id, *, limit, offset):
        def work(execute):
            total = execute("SELECT count(*) FROM jarvis_app.travel_trips WHERE owner_id = %s",
                            (owner_id,)).fetchone()[0]
            rows = execute(f"SELECT {_TRIP_COLUMNS} FROM jarvis_app.travel_trips WHERE owner_id = %s "
                           "ORDER BY start_date DESC, created_at DESC, id DESC LIMIT %s OFFSET %s",
                           (owner_id, limit, offset)).fetchall()
            return [_trip(row) for row in rows], int(total)

        return self._run(work, "Failed to list trips.")

    def update_trip(self, trip: Trip) -> bool:
        params = _trip_params(trip)
        return self._run(lambda execute: execute(
            """UPDATE jarvis_app.travel_trips SET name = %s, travellers = %s, currency = %s, budget = %s,
                   start_date = %s, end_date = %s, home_timezone = %s, daily_food = %s, daily_local_transport = %s,
                   daily_other = %s, min_connection_minutes_flight = %s, min_connection_minutes_other = %s,
                   notes = %s, updated_at = %s WHERE id = %s AND owner_id = %s""",
            params[2:15] + (trip.updated_at, trip.id, trip.owner_id)).rowcount == 1, "Failed to update the trip.")

    def delete_trip(self, owner_id, trip_id) -> bool:
        return self._run(lambda execute: execute(
            "DELETE FROM jarvis_app.travel_trips WHERE id = %s AND owner_id = %s", (trip_id, owner_id)).rowcount == 1,
            "Failed to delete the trip.")

    # ---------------------------------------------------------------- segments

    def save_segment(self, segment: Segment, *, max_segments: int):
        def work(execute):
            if execute("SELECT 1 FROM jarvis_app.travel_trips WHERE id = %s AND owner_id = %s FOR UPDATE",
                       (segment.trip_id, segment.owner_id)).fetchone() is None:
                return "missing"
            if execute("SELECT count(*) FROM jarvis_app.travel_segments WHERE trip_id = %s AND owner_id = %s",
                       (segment.trip_id, segment.owner_id)).fetchone()[0] >= max_segments:
                return "full"
            execute(f"INSERT INTO jarvis_app.travel_segments ({_SEGMENT_COLUMNS}) "
                    f"VALUES ({_placeholders(_SEGMENT_COLUMNS)})", _segment_params(segment))
            return "saved"

        return self._run(work, "Failed to store the segment.")

    def get_segment(self, owner_id, trip_id, segment_id):
        row = self._run(lambda execute: execute(
            f"SELECT {_SEGMENT_COLUMNS} FROM jarvis_app.travel_segments WHERE id = %s AND trip_id = %s AND owner_id = %s",
            (segment_id, trip_id, owner_id)).fetchone(), "Failed to read the segment.")
        return _segment(row) if row else None

    def list_segments(self, owner_id, trip_id):
        rows = self._run(lambda execute: execute(
            f"SELECT {_SEGMENT_COLUMNS} FROM jarvis_app.travel_segments WHERE trip_id = %s AND owner_id = %s "
            "ORDER BY start_utc, end_utc, id", (trip_id, owner_id)).fetchall(), "Failed to list segments.")
        return [_segment(row) for row in rows]

    def update_segment(self, segment: Segment) -> bool:
        s = segment
        return self._run(lambda execute: execute(
            """UPDATE jarvis_app.travel_segments SET kind = %s, title = %s, from_place = %s, to_place = %s,
                   start_local = %s, start_timezone = %s, end_local = %s, end_timezone = %s, start_utc = %s,
                   end_utc = %s, reference = %s, cost_amount = %s, cost_currency = %s, notes = %s, updated_at = %s
               WHERE id = %s AND trip_id = %s AND owner_id = %s""",
            (s.kind, s.title, s.from_place, s.to_place, s.start_local, s.start_timezone, s.end_local, s.end_timezone,
             s.start_utc, s.end_utc, s.reference, s.cost_amount, s.cost_currency, s.notes, s.updated_at, s.id,
             s.trip_id, s.owner_id)).rowcount == 1, "Failed to update the segment.")

    def delete_segment(self, owner_id, trip_id, segment_id) -> bool:
        return self._run(lambda execute: execute(
            "DELETE FROM jarvis_app.travel_segments WHERE id = %s AND trip_id = %s AND owner_id = %s",
            (segment_id, trip_id, owner_id)).rowcount == 1, "Failed to delete the segment.")

    # ---------------------------------------------------------------- reminder links

    def list_links(self, owner_id, trip_id):
        rows = self._run(lambda execute: execute(
            f"SELECT {_LINK_COLUMNS} FROM jarvis_app.travel_reminder_links WHERE trip_id = %s AND owner_id = %s",
            (trip_id, owner_id)).fetchall(), "Failed to list reminder links.")
        return [_link(row) for row in rows]

    def get_link(self, owner_id, segment_id):
        row = self._run(lambda execute: execute(
            f"SELECT {_LINK_COLUMNS} FROM jarvis_app.travel_reminder_links WHERE segment_id = %s AND owner_id = %s",
            (segment_id, owner_id)).fetchone(), "Failed to read the reminder link.")
        return _link(row) if row else None

    def save_link(self, link: ReminderLink) -> bool:
        """Insert unless the segment already has a link (or no longer exists); False means nothing was stored."""

        def work(execute):
            if execute("SELECT 1 FROM jarvis_app.travel_segments WHERE id = %s AND trip_id = %s AND owner_id = %s",
                       (link.segment_id, link.trip_id, link.owner_id)).fetchone() is None:
                return False
            return execute(
                f"INSERT INTO jarvis_app.travel_reminder_links ({_LINK_COLUMNS}) VALUES ({_placeholders(_LINK_COLUMNS)}) "
                "ON CONFLICT (segment_id) DO NOTHING",
                (link.owner_id, link.trip_id, link.segment_id, link.reminder_id, link.lead_minutes, link.remind_at,
                 link.created_at, link.updated_at)).rowcount == 1

        return self._run(work, "Failed to store the reminder link.")

    def update_link(self, link: ReminderLink) -> bool:
        return self._run(lambda execute: execute(
            """UPDATE jarvis_app.travel_reminder_links SET reminder_id = %s, lead_minutes = %s, remind_at = %s,
                   updated_at = %s WHERE segment_id = %s AND owner_id = %s""",
            (link.reminder_id, link.lead_minutes, link.remind_at, link.updated_at, link.segment_id,
             link.owner_id)).rowcount == 1, "Failed to update the reminder link.")

    def delete_link(self, owner_id, segment_id) -> bool:
        return self._run(lambda execute: execute(
            "DELETE FROM jarvis_app.travel_reminder_links WHERE segment_id = %s AND owner_id = %s",
            (segment_id, owner_id)).rowcount == 1, "Failed to delete the reminder link.")
