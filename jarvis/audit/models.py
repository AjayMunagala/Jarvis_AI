"""Contracts for the approval gateway and central audit trail (v2.9).

Owner decision for v2.9: approvals are **recorded only**. Approving a request
changes its state and is audited, but executes nothing; ``execution`` is always
``"not_available"``. Execution after approval requires a separately reviewed
isolation design.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

# "intelligence" (v5.x hardening, migration 0058): graph, personalization, memory-proposal and evaluation changes.
AuditCategory = Literal["tool", "browser", "approval", "intelligence"]
ApprovalState = Literal["pending", "approved", "rejected", "expired"]
ApprovalCategory = Literal["tool", "browser"]
RiskLevel = Literal["low", "medium", "high", "critical", "consequential"]
DetailValue = StrictStr | int | float | bool | None


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuditRecord(Contract):
    """Metadata-only audit record; ``durable`` says whether it reached PostgreSQL."""

    event_id: StrictStr = Field(pattern=r"^AE-[a-f0-9]{16}$")
    occurred_at: StrictStr
    owner_id: UUID
    category: AuditCategory
    event_type: StrictStr = Field(min_length=1, max_length=80)
    decision: StrictStr = Field(max_length=40)
    status: StrictStr = Field(max_length=40)
    subject: StrictStr = Field(max_length=200)
    correlation_id: StrictStr | None = Field(default=None, max_length=64)
    details: dict[StrictStr, DetailValue] = Field(default_factory=dict, max_length=12)
    durable: StrictBool


class ActivityPage(Contract):
    durable_storage: StrictBool
    storage_note: StrictStr
    events: tuple[AuditRecord, ...]


class ApprovalRequest(Contract):
    """Master-reference ApprovalRequest: action, target, reason, risk, side effects, scope, expiry, state."""

    approval_id: StrictStr = Field(pattern=r"^AR-[a-f0-9]{16}$")
    owner_id: UUID
    category: ApprovalCategory
    action: StrictStr = Field(min_length=1, max_length=80)
    target: StrictStr = Field(max_length=300)
    reason: StrictStr = Field(max_length=500)
    risk: RiskLevel
    expected_side_effects: StrictStr = Field(max_length=300)
    scope_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    correlation_id: StrictStr | None = Field(default=None, max_length=64)
    created_at: StrictStr
    expires_at: StrictStr
    state: ApprovalState
    decided_at: StrictStr | None = None
    decision_note: StrictStr | None = Field(default=None, max_length=300)
    execution: Literal["not_available"] = "not_available"
    durable: StrictBool


class ApprovalPage(Contract):
    durable_storage: StrictBool
    execution_note: StrictStr
    approvals: tuple[ApprovalRequest, ...]


class ApprovalDecisionRequest(Contract):
    note: StrictStr | None = Field(default=None, max_length=300)


class ApprovalProposal(Contract):
    """What a component asks the gateway to record (no ids, owner, or state)."""

    category: ApprovalCategory
    action: StrictStr = Field(min_length=1, max_length=80)
    target: StrictStr = Field(max_length=300)
    reason: StrictStr = Field(max_length=500)
    risk: RiskLevel
    expected_side_effects: StrictStr = Field(max_length=300)
    scope_material: StrictStr = Field(max_length=20_000)  # hashed, never stored
    correlation_id: StrictStr | None = Field(default=None, max_length=64)
