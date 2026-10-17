"""Unit tests for the v3.1 Personal Assistant subsystem.

Covers models, MemoryAssistantStore, LocalCalendarProvider, and AssistantService
audit integration.
"""

from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from jarvis.assistant.models import (
    Task,
    TaskCreateRequest,
    TaskUpdateRequest,
    TaskPriority,
    TaskStatus,
    Note,
    NoteCreateRequest,
    NoteUpdateRequest,
    Reminder,
    ReminderCreateRequest,
    ReminderUpdateRequest,
    ReminderStatus,
    CalendarEvent,
    CalendarEventCreateRequest,
    CalendarEventUpdateRequest,
)
from jarvis.assistant.stores import MemoryAssistantStore
from jarvis.assistant.calendar import LocalCalendarProvider
from jarvis.assistant.service import AssistantService
from jarvis.audit.service import AuditService
from jarvis.audit.stores import MemoryAuditStore

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


# ===================================================================
# 1. Domain Models & Validation
# ===================================================================

def test_task_models_validation():
    now = datetime.now(timezone.utc)
    create = TaskCreateRequest(
        title="Test Task",
        description="A detailed description",
        priority=TaskPriority.HIGH,
        due_date=now + timedelta(days=1),
        tags=["Work", "Urgent"],
    )
    assert create.tags == ["Work", "Urgent"]

    task = Task(
        id=uuid4(),
        owner_id=OWNER,
        title=create.title,
        description=create.description,
        priority=create.priority,
        status=TaskStatus.PENDING,
        due_date=create.due_date,
        tags=create.tags,
        created_at=now,
        updated_at=now,
    )
    assert task.title == "Test Task"
    assert task.priority == TaskPriority.HIGH
    assert task.status == TaskStatus.PENDING

    # Immutability check
    with pytest.raises(ValidationError):
        task.title = "Modified"  # type: ignore[misc]

    # Extra forbid check
    with pytest.raises(ValidationError):
        TaskCreateRequest(title="Invalid", extra_field="bad")  # type: ignore[call-arg]


def test_note_models_validation():
    now = datetime.now(timezone.utc)
    create = NoteCreateRequest(
        title="Meeting Notes",
        content="Key takeaways from the session.",
        tags=["meeting", "ai"],
    )
    assert create.tags == ["meeting", "ai"]

    note = Note(
        id=uuid4(),
        owner_id=OWNER,
        title=create.title,
        content=create.content,
        tags=create.tags,
        created_at=now,
        updated_at=now,
    )
    assert note.title == "Meeting Notes"

    with pytest.raises(ValidationError):
        note.content = "Changed"  # type: ignore[misc]


def test_reminder_models_validation():
    now = datetime.now(timezone.utc)
    remind_at = now + timedelta(hours=2)
    create = ReminderCreateRequest(
        title="Water the plants",
        remind_at=remind_at,
    )
    assert create.remind_at == remind_at

    reminder = Reminder(
        id=uuid4(),
        owner_id=OWNER,
        title=create.title,
        remind_at=create.remind_at,
        status=ReminderStatus.PENDING,
        created_at=now,
        updated_at=now,
    )
    assert reminder.status == ReminderStatus.PENDING


def test_calendar_event_models_validation():
    now = datetime.now(timezone.utc)
    start = now + timedelta(hours=1)
    end = start + timedelta(hours=1)
    create = CalendarEventCreateRequest(
        title="Standup",
        start_time=start,
        end_time=end,
        location="Room 404",
    )
    assert create.end_time > create.start_time

    # Invalid time range
    with pytest.raises(ValidationError):
        CalendarEventCreateRequest(
            title="Invalid Window",
            start_time=end,
            end_time=start,
        )


# ===================================================================
# 2. MemoryAssistantStore Tests
# ===================================================================

def test_memory_store_task_lifecycle():
    store = MemoryAssistantStore()
    now = datetime.now(timezone.utc)
    task_id = uuid4()
    task = Task(
        id=task_id,
        owner_id=OWNER,
        title="Buy Groceries",
        status=TaskStatus.PENDING,
        priority=TaskPriority.MEDIUM,
        tags=["personal"],
        created_at=now,
        updated_at=now,
    )

    store.create_task(task)

    # Retrieve
    fetched = store.get_task(OWNER, task.id)
    assert fetched is not None
    assert fetched.id == task.id

    # Cross-owner isolation
    cross = store.get_task(OTHER, task.id)
    assert cross is None

    # Update
    updated = store.update_task(OWNER, task.id, {"status": TaskStatus.COMPLETED})
    assert updated is not None
    assert updated.status == TaskStatus.COMPLETED

    # List with pagination and tag filter
    items, total = store.list_tasks(OWNER, tag="personal")
    assert total == 1
    assert items[0].id == task.id

    items_empty, total_empty = store.list_tasks(OWNER, tag="work")
    assert total_empty == 0

    # Delete
    deleted = store.delete_task(OWNER, task.id)
    assert deleted is True
    assert store.get_task(OWNER, task.id) is None


def test_memory_store_notes_and_search():
    store = MemoryAssistantStore()
    now = datetime.now(timezone.utc)
    n1 = Note(id=uuid4(), owner_id=OWNER, title="Project Alpha", content="Architecture spec", tags=["jarvis"], created_at=now, updated_at=now)
    n2 = Note(id=uuid4(), owner_id=OWNER, title="Shopping", content="Milk, eggs, coffee", tags=["food"], created_at=now, updated_at=now)

    store.create_note(n1)
    store.create_note(n2)

    # List
    items, total = store.list_notes(OWNER)
    assert total == 2

    # Query search
    items_spec, total_spec = store.list_notes(OWNER, query="spec")
    assert total_spec == 1
    assert items_spec[0].id == n1.id

    items_milk, total_milk = store.list_notes(OWNER, query="milk")
    assert total_milk == 1
    assert items_milk[0].id == n2.id

    # Update note
    upd = store.update_note(OWNER, n1.id, {"title": "Project Alpha v2"})
    assert upd is not None
    assert upd.title == "Project Alpha v2"

    # Delete
    assert store.delete_note(OWNER, n1.id) is True
    assert store.get_note(OWNER, n1.id) is None


def test_memory_store_reminders_and_calendar():
    store = MemoryAssistantStore()
    now = datetime.now(timezone.utc)
    due = now + timedelta(minutes=30)
    rem = Reminder(id=uuid4(), owner_id=OWNER, title="Check build", remind_at=due, status=ReminderStatus.PENDING, created_at=now, updated_at=now)
    store.create_reminder(rem)

    items, total = store.list_reminders(OWNER, status=ReminderStatus.PENDING)
    assert total == 1

    ev = CalendarEvent(
        id=uuid4(),
        owner_id=OWNER,
        title="Sync",
        start_time=now + timedelta(hours=2),
        end_time=now + timedelta(hours=3),
        created_at=now,
        updated_at=now,
    )
    store.create_calendar_event(ev)

    events = store.list_calendar_events(
        OWNER,
        start_range=now + timedelta(hours=1),
        end_range=now + timedelta(hours=4),
    )
    assert len(events) == 1
    assert events[0].id == ev.id


# ===================================================================
# 3. Calendar Provider Tests
# ===================================================================

def test_calendar_provider_queries_and_disclaimer():
    store = MemoryAssistantStore()
    provider = LocalCalendarProvider(store)
    now = datetime.now(timezone.utc)

    # Initially empty
    events = provider.list_events(OWNER, start_range=now, end_range=now + timedelta(days=1))
    assert len(events) == 0

    ev = provider.create_event(
        OWNER,
        CalendarEventCreateRequest(
            title="Interview",
            start_time=now + timedelta(hours=2),
            end_time=now + timedelta(hours=3),
        ),
    )
    assert ev.title == "Interview"

    # Verify disclaimer is explicit
    assert "local" in provider.description.lower()
    assert "not sync" in provider.description.lower() or "disclaim" in provider.description.lower() or "isolated" in provider.description.lower()


# ===================================================================
# 4. AssistantService and Audit Logging
# ===================================================================

def test_assistant_service_audit_emission():
    store = MemoryAssistantStore()
    audit_store = MemoryAuditStore()
    audit_service = AuditService(audit_store)
    service = AssistantService(store, audit_service)

    # Create task emits audit
    task = service.create_task(OWNER, TaskCreateRequest(title="Audit Task", tags=["Tag1", "tag1", "tag2"]))
    assert task.tags == ["tag1", "tag2"]
    entries = audit_store.recent(OWNER, limit=10)
    assert len(entries) == 1
    assert entries[0].event_type == "task.create"
    assert entries[0].category == "tool"
    assert f"assistant:task:{task.id}" in entries[0].subject

    # Update task emits audit
    service.update_task(OWNER, task.id, TaskUpdateRequest(status=TaskStatus.IN_PROGRESS))
    entries2 = audit_store.recent(OWNER, limit=10)
    assert len(entries2) == 2
    assert entries2[0].event_type == "task.update"

    # Delete task emits audit
    service.delete_task(OWNER, task.id)
    entries3 = audit_store.recent(OWNER, limit=10)
    assert len(entries3) == 3
    assert entries3[0].event_type == "task.delete"
