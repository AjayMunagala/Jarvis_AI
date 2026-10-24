"""Domain models and schemas for system health and security quarantine (v3.6)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class HealthSnapshot(StrictSchema):
    id: UUID
    owner_id: UUID
    # v4.2: None means "could not be measured", never zero (migration 0061 made the columns nullable).
    cpu_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    memory_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    disk_free_gb: float | None = Field(default=None, ge=0.0)
    services_status: dict[str, str] = Field(default_factory=dict)
    created_at: datetime


class HealthSnapshotPage(StrictSchema):
    items: list[HealthSnapshot]
    total: int = Field(ge=0)


class SecretFinding(StrictSchema):
    file_path: str = Field(min_length=1, max_length=500)
    secret_type: str = Field(min_length=1, max_length=100)
    line_number: int = Field(ge=1)
    snippet_masked: str = Field(min_length=1, max_length=200)


class DiagnosticReport(StrictSchema):
    telemetry: HealthSnapshot
    secret_findings: list[SecretFinding] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class QuarantinedFile(StrictSchema):
    id: UUID
    owner_id: UUID
    original_path: str = Field(min_length=1, max_length=500)
    quarantine_path: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=300)
    hash_sha256: str = Field(min_length=64, max_length=64)
    approved_by: UUID | None = None
    quarantined_at: datetime
    restored_at: datetime | None = None


class QuarantinedFilePage(StrictSchema):
    items: list[QuarantinedFile]
    total: int = Field(ge=0)


class QuarantineRequest(StrictSchema):
    file_path: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=300)
