"""Contracts for the bounded request -> plan -> tool -> verification loop (v2.8).

``ToolProposal`` is the only structure a model may return when proposing tool
calls. It contains no permissions, risk levels, or execution flags: trusted code
decides everything else. Arguments are a JSON *string* so the contract stays
compatible with strict structured-output schemas (no free-form objects).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

MAX_PROPOSED_CALLS_PER_ROUND = 3


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProposedCall(Contract):
    tool_id: StrictStr = Field(min_length=1, max_length=80)
    arguments_json: StrictStr = Field(max_length=4000)
    purpose: StrictStr = Field(max_length=200)


class ToolProposal(Contract):
    """Model-proposed next step. ``done=true`` with no calls ends the loop."""

    calls: tuple[ProposedCall, ...] = Field(max_length=MAX_PROPOSED_CALLS_PER_ROUND)
    done: StrictBool
    rationale: StrictStr = Field(max_length=500)


class ToolEvidenceRecord(Contract):
    """One tool outcome handed to the answering model as untrusted data."""

    ref: StrictStr = Field(pattern=r"^T[0-9]{1,2}$")
    tool_id: StrictStr
    status: StrictStr
    output_json: StrictStr = Field(max_length=6000)
    output_truncated: StrictBool = False
    error: StrictStr | None = Field(default=None, max_length=500)


Proposer = Literal["rules", "model"]


class ToolActivityCall(Contract):
    ref: StrictStr = Field(pattern=r"^T[0-9]{1,2}$")
    round: int = Field(ge=1, le=5)
    proposer: Proposer
    tool_id: StrictStr = Field(max_length=80)
    purpose: StrictStr = Field(max_length=200)
    status: StrictStr
    error_code: StrictStr | None = None
    executed: StrictBool
    evidence: tuple[StrictStr, ...] = Field(default=(), max_length=50)
    duration_ms: float = Field(ge=0)
    call_id: StrictStr | None = None
    audit_event_id: StrictStr | None = None
    untrusted_content: StrictBool = False


class VerificationCheck(Contract):
    name: StrictStr = Field(max_length=80)
    outcome: Literal["pass", "fail", "not_evaluated"]
    detail: StrictStr = Field(max_length=300)


class ToolVerification(Contract):
    status: Literal["verified", "partially_verified", "unverified", "not_applicable"]
    checks: tuple[VerificationCheck, ...] = Field(default=(), max_length=40)
    cited_refs: tuple[StrictStr, ...] = Field(default=(), max_length=20)
    unknown_citation_count: int = Field(default=0, ge=0)
    summary: StrictStr = Field(max_length=500)


class ToolActivity(Contract):
    proposer: Proposer
    rounds_used: int = Field(ge=0, le=5)
    calls: tuple[ToolActivityCall, ...] = Field(default=(), max_length=20)
    stopped_reason: Literal["no_tools_needed", "completed", "round_limit", "call_limit", "deadline",
                            "proposal_invalid", "provider_failed"]
    verification: ToolVerification | None = None
    limitations: tuple[StrictStr, ...] = Field(default=(), max_length=10)
