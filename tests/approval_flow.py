"""Test helpers for v4.3 enforced approvals: request, approve, then repeat the call with the approval ID."""

from jarvis.audit.service import ApprovalRequired, ApprovalService, AuditService
from jarvis.audit.stores import MemoryApprovalStore, MemoryAuditStore


def memory_approvals(ttl_seconds: int = 900) -> ApprovalService:
    return ApprovalService(MemoryApprovalStore(), AuditService(MemoryAuditStore()), ttl_seconds=ttl_seconds)


def approve_over_http(client, call):
    """``call(approval_id)`` makes the request; the first call must ask for approval and run nothing."""
    first = call(None)
    assert first.status_code == 202, first.text
    approval_id = first.json()["approval"]["approval_id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={}).status_code == 200
    return call(approval_id)


def approve_in_service(approvals: ApprovalService, owner_id, call):
    """Service-level variant: ``call(approval_id)`` must raise ApprovalRequired first."""
    try:
        call(None)
    except ApprovalRequired as required:
        approval_id = required.request.approval_id
    else:
        raise AssertionError("the action ran without an approval")
    approvals.decide(owner_id, approval_id, approve=True, note=None)
    return call(approval_id)
