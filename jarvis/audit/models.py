"""Contracts for the approval gateway and central audit trail (v2.9).

Owner decision for v2.9: approvals are **recorded only**. Approving a request
changes its state and is audited, but executes nothing.

Owner decision for v4.3: approvals are **enforced** for autonomous write
commands, controlled-improvement apply/restore and file quarantine/restore
(categories ``autonomous``, ``improvement`` and ``quarantine``). Those actions
do not run until a matching request is approved, and an approved request runs
its action at most once (``execution="single_use"``, ``consumed_at``). Tool and
browser requests are still recorded only (``execution="not_available"``).
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

# "intelligence" (v5.x hardening, migration 0058); "orchestration" (v4.0 capstone & v3.8 workflows, migration 0059);
# one category per v3.x subsystem (v4.2, migration 0060).
# "integration" (v4.8, migration 0065); "tutor" (v6.5, migration 0068); "travel" (v7.4, migration 0072).
AuditCategory = Literal["tool", "browser", "approval", "intelligence", "orchestration", "assistant", "workflow",
                        "knowledge", "research", "scheduler", "health", "finance", "improvement", "integration",
                        "tutor", "travel"]
ApprovalState = Literal["pending", "approved", "rejected", "expired"]
# v4.3 (migration 0062): enforced categories, whose approved action runs once.
# v4.4 (migration 0063): workflow runs with write steps.
# v4.8 (migration 0065): integration activation and state-changing device actions.
ApprovalCategory = Literal["tool", "browser", "autonomous", "improvement", "quarantine", "workflow", "integration"]
ENFORCED_CATEGORIES = frozenset({"autonomous", "improvement", "quarantine", "workflow", "integration"})
ApprovalExecution = Literal["not_available", "single_use"]
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
    execution: ApprovalExecution = "not_available"
    consumed_at: StrictStr | None = None
    durable: StrictBool


APPROVAL_ID_HELP = ("Leave empty to request approval (HTTP 202, nothing runs). After approving the request, "
                    "repeat the same call with its approval ID to run the action once.")


def execution_for(category: str) -> ApprovalExecution:
    return "single_use" if category in ENFORCED_CATEGORIES else "not_available"


class ApprovalPage(Contract):
    durable_storage: StrictBool
    execution_note: StrictStr
    approvals: tuple[ApprovalRequest, ...]


class ApprovalRequiredResponse(Contract):
    """HTTP 202 body (v4.3): the action was not run; it waits for the owner to approve ``approval``."""

    status: Literal["approval_required"] = "approval_required"
    detail: StrictStr
    approval: ApprovalRequest


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
