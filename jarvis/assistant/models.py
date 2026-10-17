"""Strict domain and API schemas for personal assistant capabilities (v3.1)."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class ReminderStatus(StrEnum):
    PENDING = "pending"
    TRIGGERED = "triggered"
    ACKNOWLEDGED = "acknowledged"
    DISMISSED = "dismissed"


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# ------------------------------------------------------------- Entities

class Task(StrictSchema):
    id: UUID
    owner_id: UUID
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.MEDIUM
    due_date: datetime | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)
    created_at: datetime
    updated_at: datetime


class Note(StrictSchema):
    id: UUID
    owner_id: UUID
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=50000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    created_at: datetime
    updated_at: datetime


class Reminder(StrictSchema):
    id: UUID
    owner_id: UUID
    task_id: UUID | None = None
    title: str = Field(min_length=1, max_length=200)
    remind_at: datetime
    status: ReminderStatus = ReminderStatus.PENDING
    created_at: datetime
    updated_at: datetime


class CalendarEvent(StrictSchema):
    id: UUID
    owner_id: UUID
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    start_time: datetime
    end_time: datetime
    location: str | None = Field(default=None, max_length=300)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_time_order(self) -> "CalendarEvent":
        if self.end_time < self.start_time:
            raise ValueError("end_time must be greater than or equal to start_time")
        return self


# ------------------------------------------------------------- Requests

class TaskCreateRequest(StrictSchema):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    priority: TaskPriority = TaskPriority.MEDIUM
    due_date: datetime | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)


class TaskUpdateRequest(StrictSchema):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    due_date: datetime | None = None
    tags: list[str] | None = Field(default=None, max_length=20)


class NoteCreateRequest(StrictSchema):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=50000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class NoteUpdateRequest(StrictSchema):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=50000)
    tags: list[str] | None = Field(default=None, max_length=20)


class ReminderCreateRequest(StrictSchema):
    title: str = Field(min_length=1, max_length=200)
    remind_at: datetime
    task_id: UUID | None = None


class ReminderUpdateRequest(StrictSchema):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    remind_at: datetime | None = None
    status: ReminderStatus | None = None


class CalendarEventCreateRequest(StrictSchema):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    start_time: datetime
    end_time: datetime
    location: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_time_order(self) -> "CalendarEventCreateRequest":
        if self.end_time < self.start_time:
            raise ValueError("end_time must be greater than or equal to start_time")
        return self


class CalendarEventUpdateRequest(StrictSchema):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    start_time: datetime | None = None
    end_time: datetime | None = None
    location: str | None = Field(default=None, max_length=300)


# ------------------------------------------------------------- Pages

class TaskPage(StrictSchema):
    items: list[Task]
    total: int = Field(ge=0)


class NotePage(StrictSchema):
    items: list[Note]
    total: int = Field(ge=0)


class ReminderPage(StrictSchema):
    items: list[Reminder]
    total: int = Field(ge=0)


class CalendarEventPage(StrictSchema):
    items: list[CalendarEvent]
    total: int = Field(ge=0)
