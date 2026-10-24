"""Domain models and schemas for controlled self-improvement and hardening (v3.9)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class PromptMutationProposal(StrictSchema):
    id: UUID
    owner_id: UUID
    target_name: str = Field(min_length=1, max_length=200)
    original_content: str
    proposed_content: str
    diff: str
    rationale: str = Field(min_length=1, max_length=1000)
    status: str = Field(default="pending", pattern=r"^(pending|approved|rejected|restored)$")
    snapshot_id: UUID | None = None
    created_at: datetime
    decided_at: datetime | None = None
    # v4.9: the evaluation run when the proposal was made (baseline vs candidate scores and regressions).
    evaluation: dict[str, Any] | None = None


class ProposalCreateRequest(StrictSchema):
    target_name: str = Field(min_length=1, max_length=200)
    proposed_content: str = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=1000)


class ProposalDecisionRequest(StrictSchema):
    decision: str = Field(pattern=r"^(approved|rejected)$")
    comment: str | None = Field(default=None, max_length=500)


class BenchmarkMetric(StrictSchema):
    name: str = Field(min_length=1, max_length=100)
    score: float = Field(ge=0.0, le=1.0)
    duration_ms: float = Field(ge=0.0)
    passed: bool
    details: str | None = None


class BenchmarkSuiteResult(StrictSchema):
    suite_id: UUID
    total_benchmarks: int = Field(ge=0)
    passed_benchmarks: int = Field(ge=0)
    all_passed: bool
    metrics: list[BenchmarkMetric] = Field(default_factory=list)
    evaluated_at: datetime


class ProposalPage(StrictSchema):
    items: list[PromptMutationProposal]
    total: int = Field(ge=0)
