"""Domain models and schemas for proactive assistance and scheduling (v3.5)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ScheduledJob(StrictSchema):
    id: UUID
    owner_id: UUID
    name: str = Field(min_length=1, max_length=200)
    schedule_type: str = Field(pattern=r"^(cron|interval|once)$")
    schedule_expr: str = Field(min_length=1, max_length=100)
    action_type: str = Field(min_length=1, max_length=50)
    payload: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str = Field(default="idle", pattern=r"^(idle|running|success|failure)$")
    created_at: datetime
    updated_at: datetime


class JobCreateRequest(StrictSchema):
    name: str = Field(min_length=1, max_length=200)
    schedule_type: str = Field(default="interval", pattern=r"^(cron|interval|once)$")
    schedule_expr: str = Field(min_length=1, max_length=100)
    action_type: str = Field(default="reminder", min_length=1, max_length=50)
    payload: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class JobUpdateRequest(StrictSchema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    schedule_type: str | None = Field(default=None, pattern=r"^(cron|interval|once)$")
    schedule_expr: str | None = Field(default=None, min_length=1, max_length=100)
    action_type: str | None = Field(default=None, min_length=1, max_length=50)
    payload: dict[str, Any] | None = None
    enabled: bool | None = None


class ScheduledJobPage(StrictSchema):
    items: list[ScheduledJob]
    total: int = Field(ge=0)


class Notification(StrictSchema):
    id: UUID
    owner_id: UUID
    category: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=300)
    message: str = Field(min_length=1)
    priority: str = Field(default="normal", pattern=r"^(low|normal|high|urgent)$")
    read: bool = False
    delivered_at: datetime | None = None
    created_at: datetime


class NotificationCreateRequest(StrictSchema):
    category: str = Field(default="assistant", min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=300)
    message: str = Field(min_length=1)
    priority: str = Field(default="normal", pattern=r"^(low|normal|high|urgent)$")


class NotificationPage(StrictSchema):
    items: list[Notification]
    total: int = Field(ge=0)


class JobRunResult(StrictSchema):
    job_id: UUID
    status: str = Field(pattern=r"^(success|failure)$")
    started_at: datetime
    completed_at: datetime
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
