"""Authenticated activity (audit) and approval-gateway endpoints (v2.9).

Approvals are recorded only: approving or rejecting changes the request's state
and is audited, but nothing is executed.
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.audit.models import ActivityPage, ApprovalDecisionRequest, ApprovalPage, ApprovalRequest, AuditCategory
from jarvis.audit.service import ApprovalError, ApprovalService, AuditService
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability

router = APIRouter(tags=["activity"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 503)}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(status_code=401, detail="A valid bearer credential is required.",
                            headers={"WWW-Authenticate": "Bearer"})
    return owner_id


def _audit(request: Request) -> AuditService:
    service = getattr(request.app.state, "audit_service", None)
    if not isinstance(service, AuditService):
        raise HTTPException(status_code=503, detail="The audit trail is unavailable.")
    return service


def _approvals(request: Request) -> ApprovalService:
    service = getattr(request.app.state, "approval_service", None)
    if not isinstance(service, ApprovalService):
        raise HTTPException(status_code=503, detail="The approval gateway is unavailable.")
    return service


async def _call(func, *args, **kwargs):
    try:
        return await run_in_threadpool(func, *args, **kwargs)
    except ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from exc


@router.get("/activity", response_model=ActivityPage, responses=_ERRORS)
async def activity(request: Request, limit: int = Query(default=50, ge=1, le=200),
                   category: AuditCategory | None = Query(default=None),
                   owner_id: UUID | None = Depends(authenticated_owner)) -> ActivityPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.AUDIT_READ)
    return await run_in_threadpool(_audit(request).page, owner, limit=limit, category=category)


@router.get("/approvals", response_model=ApprovalPage, responses=_ERRORS)
async def list_approvals(request: Request, limit: int = Query(default=50, ge=1, le=200),
                         state: Literal["pending", "approved", "rejected", "expired"] | None = Query(default=None),
                         owner_id: UUID | None = Depends(authenticated_owner)) -> ApprovalPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.APPROVALS_READ)
    return await _call(_approvals(request).list, owner, limit=limit, state=state)


@router.get("/approvals/{approval_id}", response_model=ApprovalRequest, responses=_ERRORS)
async def get_approval(approval_id: str, request: Request,
                       owner_id: UUID | None = Depends(authenticated_owner)) -> ApprovalRequest:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.APPROVALS_READ)
    return await _call(_approvals(request).get, owner, approval_id)


async def _decide(approval_id: str, body: ApprovalDecisionRequest, request: Request, owner_id, approve: bool):
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.APPROVALS_READ)
    enforce_capability(request, owner, Capability.APPROVALS_DECIDE)
    return await _call(_approvals(request).decide, owner, approval_id, approve=approve, note=body.note)


@router.post("/approvals/{approval_id}/approve", response_model=ApprovalRequest, responses=_ERRORS)
async def approve(approval_id: str, body: ApprovalDecisionRequest, request: Request,
                  owner_id: UUID | None = Depends(authenticated_owner)) -> ApprovalRequest:
    return await _decide(approval_id, body, request, owner_id, True)


@router.post("/approvals/{approval_id}/reject", response_model=ApprovalRequest, responses=_ERRORS)
async def reject(approval_id: str, body: ApprovalDecisionRequest, request: Request,
                 owner_id: UUID | None = Depends(authenticated_owner)) -> ApprovalRequest:
    return await _decide(approval_id, body, request, owner_id, False)
