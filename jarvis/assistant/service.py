"""Assistant domain service coordinating tasks, notes, reminders, and calendar (v3.1)."""

from datetime import datetime, timezone
import logging
from typing import Any
from uuid import UUID, uuid4

from jarvis.assistant.models import (
    CalendarEvent, CalendarEventCreateRequest, CalendarEventPage, CalendarEventUpdateRequest,
    Note, NoteCreateRequest, NotePage, NoteUpdateRequest,
    Reminder, ReminderCreateRequest, ReminderPage, ReminderStatus, ReminderUpdateRequest,
    Task, TaskCreateRequest, TaskPage, TaskPriority, TaskStatus, TaskUpdateRequest,
)
from jarvis.assistant.stores import AssistantStore

logger = logging.getLogger(__name__)


class AssistantService:
    def __init__(self, store: AssistantStore, audit_service: Any = None) -> None:
        self.store = store
        self.audit_service = audit_service

    def _audit(self, owner_id: UUID, event_type: str, subject: str, details: dict[str, Any]) -> None:
        if self.audit_service is not None:
            try:
                self.audit_service.record(
                    owner_id=owner_id,
                    category="tool",
                    event_type=event_type,
                    decision="allow",
                    status="success",
                    subject=subject[:200],
                    details=details,
                )
            except Exception as exc:
                logger.warning("Failed to record assistant audit event: %s", exc)

    # ------------------------------------------------------------- Tasks

    def create_task(self, owner_id: UUID, req: TaskCreateRequest) -> Task:
        now = datetime.now(timezone.utc)
        task = Task(
            id=uuid4(),
            owner_id=owner_id,
            title=req.title,
            description=req.description,
            status=TaskStatus.PENDING,
            priority=req.priority,
            due_date=req.due_date,
            tags=sorted(list({t.strip().lower() for t in req.tags if t.strip()})),
            created_at=now,
            updated_at=now,
        )
        self.store.create_task(task)
        self._audit(owner_id, "task.create", f"assistant:task:{task.id}", {"title": task.title, "priority": task.priority.value})
        return task

    def get_task(self, owner_id: UUID, task_id: UUID) -> Task | None:
        return self.store.get_task(owner_id, task_id)

    def list_tasks(
        self,
        owner_id: UUID,
        *,
        status: TaskStatus | None = None,
        priority: TaskPriority | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> TaskPage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        items, total = self.store.list_tasks(owner_id, status=status, priority=priority, tag=tag, limit=limit, offset=offset)
        return TaskPage(items=items, total=total)

    def update_task(self, owner_id: UUID, task_id: UUID, req: TaskUpdateRequest) -> Task | None:
        updates: dict[str, Any] = {}
        if req.title is not None:
            updates["title"] = req.title
        if req.description is not None:
            updates["description"] = req.description
        if req.status is not None:
            updates["status"] = req.status
        if req.priority is not None:
            updates["priority"] = req.priority
        if req.due_date is not None:
            updates["due_date"] = req.due_date
        if req.tags is not None:
            updates["tags"] = sorted(list({t.strip().lower() for t in req.tags if t.strip()}))
        updated = self.store.update_task(owner_id, task_id, updates)
        if updated:
            self._audit(owner_id, "task.update", f"assistant:task:{task_id}", {"updated_fields": list(updates.keys())})
        return updated

    def delete_task(self, owner_id: UUID, task_id: UUID) -> bool:
        deleted = self.store.delete_task(owner_id, task_id)
        if deleted:
            self._audit(owner_id, "task.delete", f"assistant:task:{task_id}", {"deleted": True})
        return deleted

    # ------------------------------------------------------------- Notes

    def create_note(self, owner_id: UUID, req: NoteCreateRequest) -> Note:
        now = datetime.now(timezone.utc)
        note = Note(
            id=uuid4(),
            owner_id=owner_id,
            title=req.title,
            content=req.content,
            tags=sorted(list({t.strip().lower() for t in req.tags if t.strip()})),
            created_at=now,
            updated_at=now,
        )
        self.store.create_note(note)
        self._audit(owner_id, "note.create", f"assistant:note:{note.id}", {"title": note.title})
        return note

    def get_note(self, owner_id: UUID, note_id: UUID) -> Note | None:
        return self.store.get_note(owner_id, note_id)

    def list_notes(
        self,
        owner_id: UUID,
        *,
        query: str | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> NotePage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        items, total = self.store.list_notes(owner_id, query=query, tag=tag, limit=limit, offset=offset)
        return NotePage(items=items, total=total)

    def update_note(self, owner_id: UUID, note_id: UUID, req: NoteUpdateRequest) -> Note | None:
        updates: dict[str, Any] = {}
        if req.title is not None:
            updates["title"] = req.title
        if req.content is not None:
            updates["content"] = req.content
        if req.tags is not None:
            updates["tags"] = sorted(list({t.strip().lower() for t in req.tags if t.strip()}))
        updated = self.store.update_note(owner_id, note_id, updates)
        if updated:
            self._audit(owner_id, "note.update", f"assistant:note:{note_id}", {"updated_fields": list(updates.keys())})
        return updated

    def delete_note(self, owner_id: UUID, note_id: UUID) -> bool:
        deleted = self.store.delete_note(owner_id, note_id)
        if deleted:
            self._audit(owner_id, "note.delete", f"assistant:note:{note_id}", {"deleted": True})
        return deleted

    # ------------------------------------------------------------- Reminders

    def create_reminder(self, owner_id: UUID, req: ReminderCreateRequest) -> Reminder:
        now = datetime.now(timezone.utc)
        reminder = Reminder(
            id=uuid4(),
            owner_id=owner_id,
            task_id=req.task_id,
            title=req.title,
            remind_at=req.remind_at,
            status=ReminderStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        self.store.create_reminder(reminder)
        self._audit(owner_id, "reminder.create", f"assistant:reminder:{reminder.id}", {"title": reminder.title, "remind_at": reminder.remind_at.isoformat()})
        return reminder

    def get_reminder(self, owner_id: UUID, reminder_id: UUID) -> Reminder | None:
        return self.store.get_reminder(owner_id, reminder_id)

    def list_reminders(
        self,
        owner_id: UUID,
        *,
        status: ReminderStatus | None = None,
        due_before: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ReminderPage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        items, total = self.store.list_reminders(owner_id, status=status, due_before=due_before, limit=limit, offset=offset)
        return ReminderPage(items=items, total=total)

    def update_reminder(self, owner_id: UUID, reminder_id: UUID, req: ReminderUpdateRequest) -> Reminder | None:
        updates: dict[str, Any] = {}
        if req.title is not None:
            updates["title"] = req.title
        if req.remind_at is not None:
            updates["remind_at"] = req.remind_at
        if req.status is not None:
            updates["status"] = req.status
        updated = self.store.update_reminder(owner_id, reminder_id, updates)
        if updated:
            self._audit(owner_id, "reminder.update", f"assistant:reminder:{reminder_id}", {"updated_fields": list(updates.keys())})
        return updated

    def delete_reminder(self, owner_id: UUID, reminder_id: UUID) -> bool:
        deleted = self.store.delete_reminder(owner_id, reminder_id)
        if deleted:
            self._audit(owner_id, "reminder.delete", f"assistant:reminder:{reminder_id}", {"deleted": True})
        return deleted

    # ------------------------------------------------------------- Calendar Events

    def create_calendar_event(self, owner_id: UUID, req: CalendarEventCreateRequest) -> CalendarEvent:
        now = datetime.now(timezone.utc)
        event = CalendarEvent(
            id=uuid4(),
            owner_id=owner_id,
            title=req.title,
            description=req.description,
            start_time=req.start_time,
            end_time=req.end_time,
            location=req.location,
            created_at=now,
            updated_at=now,
        )
        self.store.create_calendar_event(event)
        self._audit(owner_id, "calendar.create", f"assistant:calendar:{event.id}", {"title": event.title, "start_time": event.start_time.isoformat()})
        return event

    def get_calendar_event(self, owner_id: UUID, event_id: UUID) -> CalendarEvent | None:
        return self.store.get_calendar_event(owner_id, event_id)

    def list_calendar_events(
        self,
        owner_id: UUID,
        *,
        start_range: datetime,
        end_range: datetime,
        limit: int = 100,
    ) -> CalendarEventPage:
        limit = max(1, min(limit, 500))
        items = self.store.list_calendar_events(owner_id, start_range=start_range, end_range=end_range, limit=limit)
        return CalendarEventPage(items=items, total=len(items))

    def update_calendar_event(self, owner_id: UUID, event_id: UUID, req: CalendarEventUpdateRequest) -> CalendarEvent | None:
        updates: dict[str, Any] = {}
        if req.title is not None:
            updates["title"] = req.title
        if req.description is not None:
            updates["description"] = req.description
        if req.start_time is not None:
            updates["start_time"] = req.start_time
        if req.end_time is not None:
            updates["end_time"] = req.end_time
        if req.location is not None:
            updates["location"] = req.location
        updated = self.store.update_calendar_event(owner_id, event_id, updates)
        if updated:
            self._audit(owner_id, "calendar.update", f"assistant:calendar:{event_id}", {"updated_fields": list(updates.keys())})
        return updated

    def delete_calendar_event(self, owner_id: UUID, event_id: UUID) -> bool:
        deleted = self.store.delete_calendar_event(owner_id, event_id)
        if deleted:
            self._audit(owner_id, "calendar.delete", f"assistant:calendar:{event_id}", {"deleted": True})
        return deleted
