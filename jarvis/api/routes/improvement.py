"""Authenticated controlled self-improvement and hardening API routes (v3.9).

Provides prompt mutation proposal creation with diffs, immutable security gating,
approval decisions, 1-click snapshot restore, and offline benchmark evaluations.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.improvement.immutable import ImmutableSecurityFileError
from jarvis.improvement.models import (
    BenchmarkSuiteResult,
    PromptMutationProposal,
    ProposalCreateRequest,
    ProposalDecisionRequest,
    ProposalPage,
)
from jarvis.improvement.service import ImprovementService
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability

router = APIRouter(prefix="/improvement", tags=["improvement"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 503)}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return owner_id


def _service(request: Request) -> ImprovementService:
    service = getattr(request.app.state, "improvement_service", None)
    if not isinstance(service, ImprovementService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The improvement service is unavailable.",
        )
    return service


# ============================================================= Proposals

@router.post(
    "/proposals",
    response_model=PromptMutationProposal,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def propose_mutation(
    request: Request,
    body: ProposalCreateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> PromptMutationProposal:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.IMPROVEMENT_PROPOSE)
    try:
        return await run_in_threadpool(_service(request).propose_mutation, owner, body)
    except ImmutableSecurityFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.get("/proposals", response_model=ProposalPage, responses=_ERRORS)
async def list_proposals(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ProposalPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.IMPROVEMENT_READ)
    return await run_in_threadpool(
        _service(request).list_proposals, owner, limit=limit, offset=offset
    )


@router.get(
    "/proposals/{proposal_id}",
    response_model=PromptMutationProposal,
    responses=_ERRORS,
)
async def get_proposal(
    request: Request,
    proposal_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> PromptMutationProposal:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.IMPROVEMENT_READ)
    proposal = await run_in_threadpool(
        _service(request).get_proposal, owner, proposal_id
    )
    if proposal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested improvement proposal was not found.",
        )
    return proposal


@router.post(
    "/proposals/{proposal_id}/decide",
    response_model=PromptMutationProposal,
    responses=_ERRORS,
)
async def decide_proposal(
    request: Request,
    proposal_id: UUID,
    body: ProposalDecisionRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> PromptMutationProposal:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.IMPROVEMENT_APPROVE)
    try:
        return await run_in_threadpool(
            _service(request).decide_proposal, owner, proposal_id, body
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if "not found" in str(exc).lower() else status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post(
    "/proposals/{proposal_id}/restore",
    response_model=PromptMutationProposal,
    responses=_ERRORS,
)
async def restore_snapshot(
    request: Request,
    proposal_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> PromptMutationProposal:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.IMPROVEMENT_APPROVE)
    try:
        return await run_in_threadpool(
            _service(request).restore_snapshot, owner, proposal_id
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if "not found" in str(exc).lower() else status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


# ============================================================= Benchmarks

@router.post(
    "/benchmarks/run",
    response_model=BenchmarkSuiteResult,
    responses=_ERRORS,
)
async def run_benchmarks(
    request: Request,
    target_name: str | None = Query(default=None),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> BenchmarkSuiteResult:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.IMPROVEMENT_READ)
    return await run_in_threadpool(
        _service(request).evaluate_benchmarks, target_name=target_name
    )
