"""Validated contracts for bounded, advisory orchestration."""

from datetime import datetime, timezone
from typing import Literal, TypeAlias
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator
from jarvis.agent.models import ToolEvidenceRecord
from jarvis.memory.context import AssembledContext
from jarvis.memory.models import MAX_RETRIEVED_RECORDS, RetrievedMemory


IntentName = Literal[
    "general_inquiry", "recommendation", "analysis", "validation",
    "risk_assessment", "compliance", "architecture",
]
AuditEventType = Literal["execution_started", "expert_completed", "expert_failed", "execution_completed"]
EvidenceVerificationOutcome = Literal["source_match", "unverified", "not_evaluated"]
EvidenceSourceKind = Literal["user_request", "expert_output"]
MAX_SYNTHESIZED_REPLY_LENGTH = 40000


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RequestContext(Contract):
    execution_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=64)
    message: StrictStr = Field(min_length=1, max_length=20000)
    retrieved_memories: tuple[RetrievedMemory, ...] = Field(default=(), max_length=MAX_RETRIEVED_RECORDS)
    # Bounded, attributed context built by jarvis.memory.context. When absent,
    # experts assemble one from retrieved_memories with default limits.
    context: AssembledContext | None = None
    # v2.8: results of trusted-code-validated read-only tool calls for this request.
    tool_evidence: tuple[ToolEvidenceRecord, ...] = Field(default=(), max_length=20)
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class IntentClassification(Contract):
    intent: IntentName = "general_inquiry"
    confidence: float = Field(ge=0.0, le=1.0)
    rationale_code: str = Field(default="rule_match", min_length=1, max_length=40)


class AnalysisFactor(Contract):
    factor: StrictStr = Field(min_length=1, max_length=120)
    implications: StrictStr = Field(min_length=1, max_length=500)

    @field_validator("factor", "implications")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("analysis text must not be blank")
        return value


class SupportedConclusion(Contract):
    conclusion: StrictStr = Field(min_length=1, max_length=500)
    evidence: tuple[StrictStr, ...] = Field(max_length=5)

    @field_validator("conclusion")
    @classmethod
    def require_nonblank_conclusion(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("conclusions must not be blank")
        return value

    @field_validator("evidence")
    @classmethod
    def require_nonblank_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("supported conclusions require nonblank evidence")
        return value


class AnalysisExpertOutput(Contract):
    """Bounded model output for analysis; no fields represent executable actions."""

    summary: StrictStr = Field(min_length=1, max_length=40000)
    assessment_status: Literal["completed", "limited", "not_evaluated"]
    factors: tuple[AnalysisFactor, ...] = Field(max_length=8)
    supported_conclusions: tuple[SupportedConclusion, ...] = Field(max_length=8)
    assumptions: tuple[StrictStr, ...] = Field(max_length=8)
    missing_information: tuple[StrictStr, ...] = Field(max_length=8)
    recommendations: tuple[StrictStr, ...] = Field(max_length=8)

    @field_validator("summary", "assumptions", "missing_information", "recommendations")
    @classmethod
    def require_nonblank_strings(cls, value):
        values = (value,) if isinstance(value, str) else value
        if any(not item.strip() for item in values):
            raise ValueError("analysis output strings must not be blank")
        return value


class ValidationFinding(Contract):
    claim: StrictStr = Field(min_length=1, max_length=500)
    verdict: Literal["supported", "contradicted", "uncertain"]
    rationale: StrictStr = Field(min_length=1, max_length=800)
    evidence: tuple[StrictStr, ...] = Field(max_length=5)

    @field_validator("claim", "rationale")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("validation text must not be blank")
        return value

    @model_validator(mode="after")
    def supported_claims_need_evidence(self) -> "ValidationFinding":
        if self.verdict in {"supported", "contradicted"} and not self.evidence:
            raise ValueError("supported or contradicted findings require evidence")
        if any(not item.strip() for item in self.evidence):
            raise ValueError("validation evidence must not be blank")
        return self


class ValidationExpertOutput(Contract):
    """Bounded model output for claim validation; remains advisory-only."""

    summary: StrictStr = Field(min_length=1, max_length=40000)
    assessment_status: Literal["completed", "limited", "not_evaluated"]
    findings: tuple[ValidationFinding, ...] = Field(max_length=8)
    assumptions: tuple[StrictStr, ...] = Field(max_length=8)
    missing_information: tuple[StrictStr, ...] = Field(max_length=8)
    recommendations: tuple[StrictStr, ...] = Field(max_length=8)

    @field_validator("summary", "assumptions", "missing_information", "recommendations")
    @classmethod
    def require_nonblank_strings(cls, value):
        values = (value,) if isinstance(value, str) else value
        if any(not item.strip() for item in values):
            raise ValueError("validation output strings must not be blank")
        return value


ExpertExecutionOutput: TypeAlias = str | AnalysisExpertOutput | ValidationExpertOutput


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
    analysis: AnalysisExpertOutput | None = None
    validation: ValidationExpertOutput | None = None

    @model_validator(mode="after")
    def validate_specialist_payload(self) -> "ExpertOutput":
        if self.analysis is not None and self.validation is not None:
            raise ValueError("expert output cannot contain two specialist payloads")
        return self

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
        if self.status == "succeeded" and self.output is not None:
            if self.persona == "AnalysisExpert" and (self.output.analysis is None or self.output.validation is not None):
                raise ValueError("analysis experts require a validated analysis payload")
            if self.persona == "ValidationExpert" and (self.output.validation is None or self.output.analysis is not None):
                raise ValueError("validation experts require a validated validation payload")
            if self.persona not in {"AnalysisExpert", "ValidationExpert"} and (
                self.output.analysis is not None or self.output.validation is not None
            ):
                raise ValueError("only analysis and validation experts may include specialist payloads")
        return self


class EvidenceProvenance(Contract):
    """Internal evidence-location result; deliberately stores no source text."""

    step_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")
    expert_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")
    evidence_index: int = Field(ge=0)
    outcome: EvidenceVerificationOutcome
    source_kind: EvidenceSourceKind | None = None
    source_expert_id: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")

    @model_validator(mode="after")
    def validate_source(self) -> "EvidenceProvenance":
        if self.outcome == "source_match":
            if self.source_kind is None:
                raise ValueError("source matches require a source kind")
            if self.source_kind == "expert_output" and self.source_expert_id is None:
                raise ValueError("expert output matches require a source expert")
            if self.source_kind == "user_request" and self.source_expert_id is not None:
                raise ValueError("user request matches cannot name an expert")
        elif self.source_kind is not None or self.source_expert_id is not None:
            raise ValueError("unmatched evidence cannot identify a source")
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
    evidence_provenance: tuple[EvidenceProvenance, ...] = ()
    reply: StrictStr = Field(min_length=1, max_length=MAX_SYNTHESIZED_REPLY_LENGTH)
    partial: bool = False
    audit_events: tuple[AuditEvent, ...]


class ExpertExecutionError(Exception):
    """Sanitized orchestration failure safe to map to an API error."""

    def __init__(self, cause_type: str = "ExpertExecutionError"):
        self.cause_type = cause_type
        super().__init__("Configured experts failed.")


class RequestDeadlineExceeded(TimeoutError):
    """The overall chat deadline expired before expert execution began."""


class ExpertTimeoutError(ExpertExecutionError):
    """The orchestration deadline expired before useful output was available."""


class TransientExpertError(Exception):
    """An explicit retryable provider/expert failure."""
