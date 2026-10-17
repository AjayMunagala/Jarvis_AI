"""Contracts for the model registry, routing decisions and evaluations."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator

TaskType = Literal[
    "general", "reasoning", "coding", "classification", "summarization",
    "long_context_summarization", "structured_extraction", "vision", "multilingual",
]
Modality = Literal["text", "image", "audio_transcript"]
Locality = Literal["local", "remote"]
PrivacyRequirement = Literal["local_only", "prefer_local", "any"]
CapabilitySource = Literal["adapter_contract", "operator_declared", "provider_documentation"]

TASK_TYPES: tuple[str, ...] = TaskType.__args__  # type: ignore[attr-defined]
_KEY = r"^[a-z0-9][a-z0-9._-]{0,63}$"


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Pricing(Frozen):
    """Only recorded when an operator supplies a dated, sourced price."""

    currency: Literal["USD"] = "USD"
    input_per_million_tokens: float = Field(ge=0)
    output_per_million_tokens: float = Field(ge=0)
    source: StrictStr = Field(min_length=1, max_length=300)
    as_of: date


class ModelDescriptor(Frozen):
    model_key: StrictStr = Field(pattern=_KEY)
    provider: StrictStr = Field(pattern=_KEY)
    model_id: StrictStr = Field(min_length=1, max_length=200)
    display_name: StrictStr = Field(min_length=1, max_length=120)
    task_types: tuple[TaskType, ...] = Field(min_length=1)
    input_modalities: tuple[Modality, ...] = Field(min_length=1)
    # None means "unknown": unknown values never satisfy a stated requirement.
    context_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    tool_calling: StrictBool | None = None
    structured_output: StrictBool | None = None
    locality: Locality
    is_demo: StrictBool = False
    requires: tuple[StrictStr, ...] = ()
    limitations: tuple[StrictStr, ...] = ()
    pricing: Pricing | None = None
    capability_source: CapabilitySource

    @field_validator("input_modalities")
    @classmethod
    def require_text(cls, value):
        if "text" not in value:
            raise ValueError("every registered model must accept text input")
        return value


class RegistryConfig(Frozen):
    version: Literal[1]
    models: tuple[ModelDescriptor, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_keys(self) -> "RegistryConfig":
        keys = [item.model_key for item in self.models]
        if len(keys) != len(set(keys)):
            raise ValueError("model keys must be unique")
        return self


class ModelStatus(Frozen):
    model: ModelDescriptor
    available: StrictBool
    reason: StrictStr


class TaskRequirements(Frozen):
    task_type: TaskType = "general"
    modalities: tuple[Modality, ...] = ("text",)
    min_context_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    needs_structured_output: StrictBool = False
    needs_tool_calling: StrictBool = False
    privacy: PrivacyRequirement = "any"
    max_latency_ms: int | None = Field(default=None, ge=1, le=600_000)


class RoutingWeights(Frozen):
    suitability: float = Field(default=1.0, ge=0, le=10)
    quality: float = Field(default=1.0, ge=0, le=10)
    latency: float = Field(default=0.5, ge=0, le=10)
    cost: float = Field(default=0.5, ge=0, le=10)
    locality: float = Field(default=0.25, ge=0, le=10)

    @model_validator(mode="after")
    def positive_total(self) -> "RoutingWeights":
        if sum(self.model_dump().values()) <= 0:
            raise ValueError("at least one routing weight must be positive")
        return self


class RoutingPolicy(Frozen):
    name: StrictStr = Field(pattern=_KEY)
    description: StrictStr = Field(min_length=1, max_length=300)
    weights: RoutingWeights = RoutingWeights()
    allow_demo: StrictBool = True
    allow_remote: StrictBool = True
    preferred_providers: tuple[StrictStr, ...] = Field(default=(), max_length=10)
    max_fallbacks: int = Field(default=2, ge=0, le=3)


class PolicyConfig(Frozen):
    version: Literal[1]
    default_policy: StrictStr
    policies: tuple[RoutingPolicy, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def default_exists(self) -> "PolicyConfig":
        names = [item.name for item in self.policies]
        if len(names) != len(set(names)) or self.default_policy not in names:
            raise ValueError("policy names must be unique and include the default policy")
        return self


class Candidate(Frozen):
    model_key: StrictStr
    score: float
    factors: dict[str, float]
    notes: tuple[StrictStr, ...] = ()


class Exclusion(Frozen):
    model_key: StrictStr
    reasons: tuple[StrictStr, ...] = Field(min_length=1)


class RoutingDecision(Frozen):
    decision_id: StrictStr = Field(pattern=r"^RD-[a-f0-9]{16}$")
    policy: StrictStr
    requirements: TaskRequirements
    selected: StrictStr | None
    fallbacks: tuple[StrictStr, ...] = ()
    ranked: tuple[Candidate, ...] = ()
    excluded: tuple[Exclusion, ...] = ()
    demo: StrictBool = False
    explanation: StrictStr


class Attempt(Frozen):
    model_key: StrictStr
    outcome: Literal["succeeded", "failed", "timeout"]
    duration_ms: float = Field(ge=0)
    error_code: StrictStr | None = None


class RoutedReply(Frozen):
    decision_id: StrictStr
    model_key: StrictStr | None
    content: StrictStr | None
    attempts: tuple[Attempt, ...]
    status: Literal["succeeded", "failed", "no_eligible_model"]
    demo: StrictBool = False


class RoutingRecord(Frozen):
    """Observability record: metadata only, never prompt or reply text."""

    decision_id: StrictStr
    occurred_at: StrictStr
    task_type: TaskType
    policy: StrictStr
    selected: StrictStr | None
    attempts: tuple[Attempt, ...] = ()
    status: StrictStr
    prompt_sha256: StrictStr | None = None
    prompt_chars: int | None = None


# ------------------------------------------------------------- evaluation

FieldType = Literal["string", "integer", "number", "boolean"]


class Expectation(Frozen):
    kind: Literal["exact", "contains_all", "regex", "number", "json_fields"]
    value: StrictStr | None = None
    values: tuple[StrictStr, ...] = ()
    number: float | None = None
    tolerance: float = Field(default=0.0, ge=0)
    fields: dict[str, FieldType] = Field(default_factory=dict)
    field_values: dict[str, StrictStr | int | float | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent(self) -> "Expectation":
        needed = {"exact": self.value is not None, "regex": self.value is not None,
                  "contains_all": bool(self.values), "number": self.number is not None,
                  "json_fields": bool(self.fields)}
        if not needed[self.kind]:
            raise ValueError(f"expectation kind {self.kind!r} is missing its value")
        return self


class EvaluationCase(Frozen):
    case_id: StrictStr = Field(pattern=_KEY)
    task_type: TaskType
    prompt: StrictStr = Field(min_length=1, max_length=8000)
    system_instruction: StrictStr | None = Field(default=None, max_length=4000)
    expected: Expectation


class EvaluationDataset(Frozen):
    dataset_id: StrictStr = Field(pattern=_KEY)
    version: int = Field(ge=1)
    description: StrictStr = Field(min_length=1, max_length=500)
    cases: tuple[EvaluationCase, ...] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_cases(self) -> "EvaluationDataset":
        ids = [item.case_id for item in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case identifiers must be unique")
        return self


class CaseResult(Frozen):
    case_id: StrictStr
    model_key: StrictStr
    repeat: int = Field(ge=1)
    completed: StrictBool
    correct: StrictBool
    structured_valid: StrictBool | None = None
    latency_ms: float = Field(ge=0)
    error_code: StrictStr | None = None


class ModelEvaluationSummary(Frozen):
    model_key: StrictStr
    runs: int = Field(ge=0)
    completion_rate: float = Field(ge=0, le=1)
    correctness_rate: float = Field(ge=0, le=1)
    structured_validity_rate: float | None = Field(default=None, ge=0, le=1)
    failure_rate: float = Field(ge=0, le=1)
    p50_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    estimated_cost_usd: float | None = None
    cost_note: StrictStr
    simulated: StrictBool


class EvaluationReport(Frozen):
    report_id: StrictStr = Field(pattern=r"^ER-[a-f0-9]{16}$")
    dataset_id: StrictStr
    dataset_version: int
    dataset_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    started_at: StrictStr
    repeats: int = Field(ge=1)
    summaries: tuple[ModelEvaluationSummary, ...]
    results: tuple[CaseResult, ...]
    notes: tuple[StrictStr, ...] = ()
