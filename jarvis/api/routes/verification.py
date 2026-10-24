"""Authenticated verification and quality engineering API endpoints."""

from time import monotonic
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability
from jarvis.verification.models import (
    RunVerificationRequest,
    VerificationPlan,
    VerificationPlanRequest,
    VerificationProfilesResponse,
    VerificationReport,
    VerifyPatchRequest,
)
from jarvis.verification.service import VerificationService

router = APIRouter(prefix="/verification", tags=["verification"])


def _get_verification_service(request: Request) -> VerificationService:
    service = getattr(request.app.state, "verification_service", None)
    repo_service = getattr(request.app.state, "repo_service", None)
    if repo_service and hasattr(repo_service, "repo_root"):
        if not isinstance(service, VerificationService) or service.repo_root != repo_service.repo_root:
            service = VerificationService(repo_service.repo_root, request.app.state.settings)
            request.app.state.verification_service = service
    elif not isinstance(service, VerificationService):
        service = VerificationService(".", request.app.state.settings)
        request.app.state.verification_service = service
    return service


@router.post(
    "/plan",
    response_model=VerificationPlan,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 413, 422, 503)},
)
async def create_plan(
    body: VerificationPlanRequest,
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> VerificationPlan:
    if owner_id is None:
        raise HTTPException(
            status_code=401,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    enforce_capability(request, owner_id, Capability.VERIFY_READ)
    service = _get_verification_service(request)
    try:
        return service.create_plan(body)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Verification planning is unavailable.") from exc


@router.post(
    "/verify-patch",
    response_model=VerificationReport,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 413, 422, 503)},
)
async def verify_patch(
    body: VerifyPatchRequest,
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> VerificationReport:
    if owner_id is None:
        raise HTTPException(
            status_code=401,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    enforce_capability(request, owner_id, Capability.VERIFY_READ)
    service = _get_verification_service(request)
    try:
        return service.verify_patch(body)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Patch verification is unavailable.") from exc


@router.post(
    "/run",
    response_model=VerificationReport,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 413, 422, 503, 504)},
)
async def run_verification(
    body: RunVerificationRequest,
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> VerificationReport:
    if owner_id is None:
        raise HTTPException(
            status_code=401,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    enforce_capability(request, owner_id, Capability.VERIFY_EXECUTE)
    service = _get_verification_service(request)

    # Mandatory Dual-Gate Policy: VERIFY_EXECUTE alone cannot enable execution without server setting
    if not service.execution_enabled:
        raise HTTPException(
            status_code=403,
            detail="Verification execution is disabled by server policy (verification_execution_enabled=False).",
        )

    deadline = monotonic() + request.app.state.settings.request_timeout_seconds
    try:
        return await service.run_plan(
            body.plan_id,
            check_ids=body.check_ids,
            deadline=deadline,
        )
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Verification execution timed out.") from None
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Verification execution is unavailable.") from exc


@router.get(
    "/profiles",
    response_model=VerificationProfilesResponse,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 503)},
)
async def list_profiles(
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> VerificationProfilesResponse:
    if owner_id is None:
        raise HTTPException(
            status_code=401,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    enforce_capability(request, owner_id, Capability.VERIFY_READ)
    service = _get_verification_service(request)
    return VerificationProfilesResponse(
        execution_enabled=service.execution_enabled,
        profiles=service.get_supported_profiles(),
    )
