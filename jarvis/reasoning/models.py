"""Contracts for plans under evaluation, findings, uncertainty and refinement."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator

ActionKind = Literal["analysis", "computation", "read_only_tool", "communication", "consequential"]
CheckName = Literal["goal_coverage", "constraints", "dependencies", "information", "redundancy", "completion", "safety"]
CHECKS: tuple[str, ...] = CheckName.__args__  # type: ignore[attr-defined]
_ID = r"^[a-z0-9][a-z0-9_-]{0,39}$"
_NAME = r"^[a-z0-9][a-z0-9_.-]{0,59}$"


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PlanStepSpec(Frozen):
    step_id: StrictStr = Field(pattern=_ID)
    description: StrictStr = Field(min_length=1, max_length=500)
    action_kind: ActionKind = "analysis"
    tool_id: StrictStr | None = Field(default=None, max_length=80)
    depends_on: tuple[StrictStr, ...] = Field(default=(), max_length=20)
    inputs: tuple[StrictStr, ...] = Field(default=(), max_length=20)
    outputs: tuple[StrictStr, ...] = Field(default=(), max_length=20)
    completion_criteria: StrictStr = Field(default="", max_length=300)
    requires_approval: StrictBool = False


class PlanConstraints(Frozen):
    max_steps: int | None = Field(default=None, ge=1, le=40)
    forbidden_action_kinds: tuple[ActionKind, ...] = ()
    forbidden_tools: tuple[StrictStr, ...] = Field(default=(), max_length=20)
    required_step_terms: tuple[StrictStr, ...] = Field(default=(), max_length=10)
    available_inputs: tuple[StrictStr, ...] = Field(default=(), max_length=40)


class PlanCandidate(Frozen):
    goal: StrictStr = Field(min_length=1, max_length=2000)
    steps: tuple[PlanStepSpec, ...] = Field(min_length=1, max_length=40)
    constraints: PlanConstraints = PlanConstraints()
    assumptions: tuple[StrictStr, ...] = Field(default=(), max_length=20)


class Finding(Frozen):
    check: CheckName
    severity: Literal["error", "warning", "info"]
    message: StrictStr
    step_id: StrictStr | None = None


class UncertaintyAssessment(Frozen):
    level: Literal["low", "medium", "high"]
    missing: tuple[StrictStr, ...] = ()
    conflicting: tuple[StrictStr, ...] = ()
    notes: tuple[StrictStr, ...] = ()


class PlanEvaluation(Frozen):
    verdict: Literal["pass", "needs_revision", "reject"]
    score: float = Field(ge=0, le=1)
    checks: dict[str, bool]
    findings: tuple[Finding, ...]
    uncertainty: UncertaintyAssessment
    evaluator: StrictStr
    note: StrictStr = "Evaluation checks plan quality only; nothing has been executed."


class EvidenceItem(Frozen):
    claim_id: StrictStr = Field(pattern=_NAME)
    source_ref: StrictStr = Field(min_length=1, max_length=300)
    stance: Literal["supports", "contradicts"]
    reliability: Literal["verified", "user_stated", "document", "inferred"]


class ClaimAssessment(Frozen):
    claim_id: StrictStr
    status: Literal["supported", "contested", "unsupported"]
    supporting: tuple[StrictStr, ...] = ()
    contradicting: tuple[StrictStr, ...] = ()


class VerifierVerdict(Frozen):
    verifier_id: StrictStr = Field(min_length=1, max_length=80)
    kind: Literal["deterministic", "model"]
    outcome: Literal["agree", "disagree", "uncertain"]
    reasons: tuple[StrictStr, ...] = ()


class ConsolidatedVerdict(Frozen):
    status: Literal["verified", "plausible_unverified", "contested", "failed"]
    verdicts: tuple[VerifierVerdict, ...]
    explanation: StrictStr


class RefinementAttempt(Frozen):
    attempt: int = Field(ge=0)
    verdict: StrictStr
    score: float
    error_count: int = Field(ge=0)


class RefinementResult(Frozen):
    final_plan: PlanCandidate
    final_evaluation: PlanEvaluation
    attempts: tuple[RefinementAttempt, ...]
    stopped_reason: Literal["passed", "max_attempts", "no_progress", "refiner_error", "rejected", "timeout"]
    executed: Literal[False] = False


class PlanCase(Frozen):
    case_id: StrictStr = Field(pattern=_NAME)
    plan: PlanCandidate
    expected_verdict: Literal["pass", "needs_revision", "reject"]
    expected_failed_checks: tuple[CheckName, ...] = ()


class PlanDataset(Frozen):
    dataset_id: StrictStr = Field(pattern=_NAME)
    version: int = Field(ge=1)
    description: StrictStr
    cases: tuple[PlanCase, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique(self) -> "PlanDataset":
        ids = [item.case_id for item in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case identifiers must be unique")
        return self
