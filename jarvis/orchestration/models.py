"""Validated contracts for bounded, advisory orchestration."""

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator


IntentName = Literal[
    "general_inquiry", "recommendation", "analysis", "validation",
    "risk_assessment", "compliance", "architecture",
]
AuditEventType = Literal["execution_started", "expert_completed", "expert_failed", "execution_completed"]
MAX_SYNTHESIZED_REPLY_LENGTH = 40000


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RequestContext(Contract):
    execution_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=64)
    message: StrictStr = Field(min_length=1, max_length=20000)
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class IntentClassification(Contract):
    intent: IntentName = "general_inquiry"
    confidence: float = Field(ge=0.0, le=1.0)
    rationale_code: str = Field(default="rule_match", min_length=1, max_length=40)


class PlanStep(Contract):
    step_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")
    expert_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")
    round_number: int = Field(ge=1, le=5)
    depends_on: tuple[str, ...] = ()
    risk_class: Literal["low", "medium", "high"] = "low"
    expected_evidence: str = Field(min_length=1, max_length=240)


class ExecutionPlan(Contract):
    execution_id: str = Field(min_length=1, max_length=64)
    goal: StrictStr = Field(min_length=1, max_length=20000)
    intent: IntentName
    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=25)
    max_rounds: int = Field(default=1, ge=1, le=5)
    allow_parallel: bool = False
    max_concurrency: int = Field(default=1, ge=1, le=3)
    stop_condition: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_rounds_and_dependencies(self) -> "ExecutionPlan":
        step_ids = {step.step_id for step in self.steps}
        if len(step_ids) != len(self.steps):
            raise ValueError("plan step identifiers must be unique")
        if max(step.round_number for step in self.steps) > self.max_rounds:
            raise ValueError("a plan step exceeds max_rounds")
        if not self.allow_parallel and self.max_concurrency != 1:
            raise ValueError("sequential plans must use max_concurrency=1")
        if any(dep not in step_ids for step in self.steps for dep in step.depends_on):
            raise ValueError("plan dependencies must reference a known step")
        step_rounds = {step.step_id: step.round_number for step in self.steps}
        for step in self.steps:
            if step.step_id in step.depends_on:
                raise ValueError("a plan step cannot depend on itself")
            if any(step_rounds[dependency] >= step.round_number for dependency in step.depends_on):
                raise ValueError("dependencies must be completed in an earlier round")
        return self


class ExpertOutput(Contract):
    content: StrictStr = Field(min_length=1, max_length=40000)
    evidence: tuple[StrictStr, ...] = Field(default=(), max_length=10)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    advisory_only: Literal[True] = True

    @field_validator("content")
    @classmethod
    def content_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("expert output content must contain non-whitespace text")
        return value


class ExpertResult(Contract):
    step_id: str
    expert_id: str
    persona: str
    status: Literal["succeeded", "failed"]
    output: ExpertOutput | None = None
    error_code: Literal["transient_failure", "timeout", "malformed_output", "expert_failure"] | None = None
    failure_type: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    attempts: int = Field(ge=1, le=4)

    @model_validator(mode="after")
    def validate_result_state(self) -> "ExpertResult":
        if self.status == "succeeded" and (self.output is None or self.error_code is not None or self.failure_type is not None):
            raise ValueError("successful expert results require output only")
        if self.status == "failed" and (self.output is not None or self.error_code is None):
            raise ValueError("failed expert results require an error code only")
        return self


class AuditEvent(Contract):
    execution_id: str
    event_type: AuditEventType
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expert_id: str | None = None
    outcome: Literal["started", "succeeded", "failed", "partial"]
    # Deliberately excludes message, prompt, provider output and exception text.


class OrchestrationResponse(Contract):
    execution_id: str
    intent: IntentClassification
    plan: ExecutionPlan
    expert_results: tuple[ExpertResult, ...]
    reply: StrictStr = Field(min_length=1, max_length=MAX_SYNTHESIZED_REPLY_LENGTH)
    partial: bool = False
    audit_events: tuple[AuditEvent, ...]


class ExpertExecutionError(Exception):
    """Sanitized orchestration failure safe to map to an API error."""

    def __init__(self, cause_type: str = "ExpertExecutionError"):
        self.cause_type = cause_type
        super().__init__("Configured experts failed.")


class ExpertTimeoutError(ExpertExecutionError):
    """The orchestration deadline expired before useful output was available."""


class TransientExpertError(Exception):
    """An explicit retryable provider/expert failure."""
