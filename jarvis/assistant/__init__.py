"""Personal assistant capabilities package (v3.1)."""

from jarvis.assistant.models import (
    CalendarEvent, CalendarEventCreateRequest, CalendarEventPage, CalendarEventUpdateRequest,
    Note, NoteCreateRequest, NotePage, NoteUpdateRequest,
    Reminder, ReminderCreateRequest, ReminderPage, ReminderStatus, ReminderUpdateRequest,
    Task, TaskCreateRequest, TaskPage, TaskPriority, TaskStatus, TaskUpdateRequest,
)
from jarvis.assistant.service import AssistantService
from jarvis.assistant.stores import (
    AssistantStorageError, AssistantStore, MemoryAssistantStore, PostgresAssistantStore,
)

__all__ = [
    "Task", "TaskStatus", "TaskPriority", "TaskCreateRequest", "TaskUpdateRequest", "TaskPage",
    "Note", "NoteCreateRequest", "NoteUpdateRequest", "NotePage",
    "Reminder", "ReminderStatus", "ReminderCreateRequest", "ReminderUpdateRequest", "ReminderPage",
    "CalendarEvent", "CalendarEventCreateRequest", "CalendarEventUpdateRequest", "CalendarEventPage",
    "AssistantStore", "MemoryAssistantStore", "PostgresAssistantStore", "AssistantStorageError",
    "AssistantService",
]
