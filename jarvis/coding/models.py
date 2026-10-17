"""Closed, bounded contracts for coding requests and untrusted proposals."""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator

Text = Annotated[StrictStr, Field(min_length=1, max_length=1000)]
PathText = Annotated[StrictStr, Field(min_length=1, max_length=240)]
Workflow = Literal["explain", "locate", "trace", "diagnose", "recommend", "patch", "review_patch", "tests"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Replacement(Contract):
    old_text: StrictStr = Field(min_length=1, max_length=4000)
    new_text: StrictStr = Field(max_length=4000)


class FileChange(Contract):
    path: PathText
    operation: Literal["modify", "create"]
    replacements: tuple[Replacement, ...] = Field(default=(), max_length=8)
    content: StrictStr | None = Field(default=None, max_length=8000)
    reason: Text

    @model_validator(mode="after")
    def consistent_operation(self):
        if self.operation == "modify" and (not self.replacements or self.content is not None):
            raise ValueError("Modifications require exact replacements only.")
        if self.operation == "create" and (self.content is None or self.replacements):
            raise ValueError("Creation requires content only.")
        return self


class CodingRequest(Contract):
    request: StrictStr = Field(min_length=1, max_length=4000)
    workflow: Literal["auto", "explain", "locate", "trace", "diagnose", "recommend", "patch", "review_patch", "tests"] = "auto"
    target_paths: tuple[PathText, ...] = Field(default=(), max_length=8)
    reported_observation: StrictStr | None = Field(default=None, max_length=8000)
    changes: tuple[FileChange, ...] = Field(default=(), max_length=5)
    allow_provider_context: StrictBool = False

    @model_validator(mode="after")
    def nonblank_request(self):
        if not self.request.strip():
            raise ValueError("A coding request is required.")
        return self


class Evidence(Contract):
    id: StrictStr = Field(pattern=r"^E[1-8]$")
    path: PathText
    sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    excerpt: StrictStr = Field(max_length=1600)
    excerpt_start_line: int = Field(ge=1)
    truncated: bool
    symbols: tuple[Text, ...] = Field(max_length=20)
    relationships: tuple[Text, ...] = Field(max_length=20)


class Hypothesis(Contract):
    explanation: Text
    evidence_ids: tuple[StrictStr, ...] = Field(max_length=8)


class Proposal(Contract):
    summary: StrictStr = Field(min_length=1, max_length=2000)
    hypotheses: tuple[Hypothesis, ...] = Field(default=(), max_length=8)
    recommendations: tuple[Text, ...] = Field(default=(), max_length=8)
    risks: tuple[Text, ...] = Field(default=(), max_length=8)
    missing_information: tuple[Text, ...] = Field(default=(), max_length=8)
    changes: tuple[FileChange, ...] = Field(default=(), max_length=5)


class ModelFileChange(Contract):
    """All fields required for the existing strict structured-output adapter."""

    path: PathText
    operation: Literal["modify", "create"]
    replacements: tuple[Replacement, ...] = Field(max_length=8)
    content: StrictStr | None = Field(max_length=8000)
    reason: Text


class ModelProposal(Contract):
    """Wire shape has no defaults; local Proposal still enforces edit semantics."""

    summary: StrictStr = Field(min_length=1, max_length=2000)
    hypotheses: tuple[Hypothesis, ...] = Field(max_length=8)
    recommendations: tuple[Text, ...] = Field(max_length=8)
    risks: tuple[Text, ...] = Field(max_length=8)
    missing_information: tuple[Text, ...] = Field(max_length=8)
    changes: tuple[ModelFileChange, ...] = Field(max_length=5)


class PatchFile(Contract):
    path: PathText
    operation: Literal["modify", "create"]
    reason: Text
    sha256_before: StrictStr | None
    sha256_after: StrictStr
    diff: StrictStr = Field(min_length=1, max_length=16000)


class CodingReport(Contract):
    execution_id: str
    workflow: Workflow
    interpretation: Text
    analysis_origin: Literal["static", "model"]
    provider: str
    evidence: tuple[Evidence, ...] = Field(max_length=8)
    facts: tuple[Text, ...] = Field(max_length=16)
    reported_observation: StrictStr | None = Field(max_length=8000)
    proposal: Proposal
    patch_status: Literal["not_requested", "proposed", "unavailable"]
    patches: tuple[PatchFile, ...] = Field(max_length=5)
    existing_tests: tuple[PathText, ...] = Field(max_length=20)
    verification_recommendations: tuple[Text, ...] = Field(max_length=8)
    limitations: tuple[Text, ...] = Field(max_length=12)
    verification_status: Literal["not_run"] = "not_run"
    repository_modified: Literal[False] = False
    execution_enabled: Literal[False] = False
