"""Domain models for Autonomous AI Operating System Coordination (v4.0)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class AutonomousCommandRequest(StrictSchema):
    prompt: str = Field(min_length=1, max_length=1000)
    context: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False
    max_steps: int = Field(default=5, ge=1, le=20)
    # v4.3: a command with write steps runs only with the ID of an approved request for exactly this command.
    approval_id: str | None = Field(default=None, pattern=r"^AR-[a-f0-9]{16}$")


class AutonomousStepLog(StrictSchema):
    step_index: int
    subsystem: str
    action: str
    status: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class AutonomousCommandResult(StrictSchema):
    command_id: UUID
    owner_id: UUID
    prompt: str
    status: str
    intent: str
    subsystems_involved: list[str]
    steps: list[AutonomousStepLog]
    summary: str
    executed_at: datetime
    approval_id: str | None = None


class SubsystemStatus(StrictSchema):
    name: str
    operational: bool
    metrics: dict[str, Any] = Field(default_factory=dict)
    details: str | None = None


class SystemOverview(StrictSchema):
    system_status: str
    subsystems: dict[str, SubsystemStatus]
    active_features: list[str]
    version: str = "4.0.0"
    timestamp: datetime
