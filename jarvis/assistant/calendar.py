"""Calendar abstraction and local provider implementation (v3.1).

This module provides a decoupled CalendarProvider protocol. In v3.1, operations
are strictly local. No external calendar synchronization or remote writes occur.
"""

from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID, uuid4

from jarvis.assistant.models import CalendarEvent, CalendarEventCreateRequest
from jarvis.assistant.stores import AssistantStore


class CalendarProvider(Protocol):
    """Protocol for calendar event storage and queries."""

    def list_events(
        self,
        owner_id: UUID,
        *,
        start_range: datetime,
        end_range: datetime,
        limit: int = 100,
    ) -> list[CalendarEvent]: ...

    def get_event(self, owner_id: UUID, event_id: UUID) -> CalendarEvent | None: ...

    def create_event(
        self, owner_id: UUID, request: CalendarEventCreateRequest
    ) -> CalendarEvent: ...

    def delete_event(self, owner_id: UUID, event_id: UUID) -> bool: ...


class LocalCalendarProvider:
    """Strictly local calendar provider backed by AssistantStore.

    Disclaims external sync: all calendar data remains strictly local and isolated
    in JARVIS storage. External calendar synchronization is intentionally disclaimed in v3.1.
    """

    description: str = "Local isolated calendar provider. Does not sync with external providers."

    def __init__(self, store: AssistantStore) -> None:
        self._store = store

    def list_events(
        self,
        owner_id: UUID,
        *,
        start_range: datetime,
        end_range: datetime,
        limit: int = 100,
    ) -> list[CalendarEvent]:
        return self._store.list_calendar_events(
            owner_id=owner_id,
            start_range=start_range,
            end_range=end_range,
            limit=limit,
        )

    def get_event(self, owner_id: UUID, event_id: UUID) -> CalendarEvent | None:
        return self._store.get_calendar_event(owner_id=owner_id, event_id=event_id)

    def create_event(
        self, owner_id: UUID, request: CalendarEventCreateRequest
    ) -> CalendarEvent:
        now = datetime.now(timezone.utc)
        event = CalendarEvent(
            id=uuid4(),
            owner_id=owner_id,
            title=request.title,
            description=request.description,
            start_time=request.start_time,
            end_time=request.end_time,
            location=request.location,
            created_at=now,
            updated_at=now,
        )
        self._store.create_calendar_event(event)
        return event

    def delete_event(self, owner_id: UUID, event_id: UUID) -> bool:
        return self._store.delete_calendar_event(owner_id=owner_id, event_id=event_id)
