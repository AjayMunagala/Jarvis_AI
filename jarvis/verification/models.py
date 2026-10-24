"""Closed, bounded contracts for software verification and evidence-based validation."""

from enum import StrEnum
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from jarvis.coding.models import PatchFile

Text = Annotated[StrictStr, Field(min_length=1, max_length=1000)]
PathText = Annotated[StrictStr, Field(min_length=1, max_length=240)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VerificationState(StrEnum):
    NOT_CHECKED = "not_checked"
    CHECK_REQUESTED = "check_requested"
    CHECK_RUNNING = "check_running"
    CHECK_PASSED = "check_passed"
    CHECK_FAILED = "check_failed"
    CHECK_TIMED_OUT = "check_timed_out"
    CHECK_BLOCKED = "check_blocked"
    CHECK_RESULT_UNAVAILABLE = "check_result_unavailable"
    INCONCLUSIVE = "inconclusive"


class CheckScope(StrEnum):
    SYNTAX = "syntax"
    UNIT = "unit"
    INTEGRATION = "integration"
    TYPECHECK = "typecheck"
    BUILD = "build"
    REGRESSION = "regression"


class CheckPlanItem(Contract):
    check_id: StrictStr = Field(pattern=r"^CHK-[a-f0-9]{6,16}$")
    name: Text
    scope: CheckScope
    target: PathText
    profile_id: StrictStr = Field(min_length=1, max_length=50)
    rationale: Text
    expected_files: tuple[PathText, ...] = Field(default=(), max_length=10)


class VerificationPlan(Contract):
    plan_id: StrictStr = Field(pattern=r"^VP-[a-f0-9]{8,16}$")
    target_paths: tuple[PathText, ...] = Field(max_length=10)
    checks: tuple[CheckPlanItem, ...] = Field(max_length=10)
    rationale: Text
    execution_enabled: StrictBool
    created_at: StrictStr


class VerificationEvidence(Contract):
    evidence_id: StrictStr = Field(pattern=r"^VE-[a-f0-9]{8,16}$")
    check_id: StrictStr = Field(pattern=r"^CHK-[a-f0-9]{6,16}$")
    check_name: Text
    profile_id: StrictStr = Field(min_length=1, max_length=50)
    state: VerificationState
    command_executed: tuple[StrictStr, ...] | None = Field(default=None, max_length=20)
    exit_code: int | None = None
    started_at: StrictStr
    completed_at: StrictStr | None = None
    duration_seconds: float = Field(ge=0.0)
    output_excerpt: StrictStr = Field(default="", max_length=16000)
    output_truncated: StrictBool = False
    redacted_error_details: StrictStr | None = Field(default=None, max_length=4000)
    verified_files: tuple[PathText, ...] = Field(default=(), max_length=20)
    provenance: Literal["direct_execution", "static_analysis", "reported_external"]
    limitations: tuple[Text, ...] = Field(default=(), max_length=10)


class CheckResult(Contract):
    check_id: StrictStr = Field(pattern=r"^CHK-[a-f0-9]{6,16}$")
    name: Text
    scope: CheckScope
    state: VerificationState
    evidence: VerificationEvidence
    summary: Text
    diagnostics: tuple[Text, ...] = Field(default=(), max_length=10)


class VerificationReport(Contract):
    execution_id: StrictStr
    plan_id: StrictStr = Field(pattern=r"^VP-[a-f0-9]{8,16}$")
    overall_state: VerificationState
    results: tuple[CheckResult, ...] = Field(max_length=10)
    summary: StrictStr = Field(max_length=2000)
    facts: tuple[Text, ...] = Field(default=(), max_length=16)
    limitations: tuple[Text, ...] = Field(default=(), max_length=12)
    execution_enabled: StrictBool
    repository_modified: Literal[False] = False


class ProfileDefinition(Contract):
    profile_id: StrictStr = Field(min_length=1, max_length=50)
    name: Text
    description: Text
    working_directory: Literal[".", "frontend"]
    allowed_target_dirs: tuple[PathText, ...] = Field(max_length=10)
    default_timeout_seconds: float = Field(ge=1.0, le=120.0)


class VerificationPlanRequest(Contract):
    target_paths: tuple[PathText, ...] = Field(default=(), max_length=10)
    reason: Text = "Verification plan requested"


class VerifyPatchRequest(Contract):
    patches: tuple[PatchFile, ...] = Field(min_length=1, max_length=5)
    reported_observation: StrictStr | None = Field(default=None, max_length=8000)


class RunVerificationRequest(Contract):
    plan_id: StrictStr = Field(pattern=r"^VP-[a-f0-9]{8,16}$")
    check_ids: tuple[StrictStr, ...] = Field(default=(), max_length=10)


class VerificationProfilesResponse(Contract):
    execution_enabled: StrictBool
    profiles: tuple[ProfileDefinition, ...]
