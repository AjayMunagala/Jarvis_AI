"""Authenticated integration and device-readiness routes (v4.8). Only a mock adapter exists."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.audit.models import APPROVAL_ID_HELP, ApprovalRequiredResponse
from jarvis.integrations.models import (ActionRequest, ActionResult, AdapterDescriptor, ENROLLMENT_ID_PATTERN,
                                        EnrollmentCreateRequest, EnrollmentPage, EnrollmentView)
from jarvis.integrations.service import IntegrationError, IntegrationService
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability

router = APIRouter(prefix="/integrations", tags=["integrations"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 503)}
_GATED = {**_ERRORS, 202: {"model": ApprovalRequiredResponse, "description": "Approval required; nothing was executed."}}
EnrollmentId = Path(pattern=ENROLLMENT_ID_PATTERN)


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(status_code=401, detail="A valid bearer credential is required.",
                            headers={"WWW-Authenticate": "Bearer"})
    return owner_id


def _service(request: Request) -> IntegrationService:
    service = getattr(request.app.state, "integration_service", None)
    if not isinstance(service, IntegrationService):
        raise HTTPException(status_code=503, detail="Integrations are unavailable.")
    return service


async def _call(func, *args, **kwargs):
    try:
        return await run_in_threadpool(func, *args, **kwargs)
    except IntegrationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from exc


@router.get("/adapters", response_model=tuple[AdapterDescriptor, ...], responses=_ERRORS)
async def list_adapters(request: Request, owner_id: UUID | None = Depends(authenticated_owner)):
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.INTEGRATIONS_READ)
    return _service(request).list_adapters()


@router.get("/enrollments", response_model=EnrollmentPage, responses=_ERRORS)
async def list_enrollments(request: Request, limit: int = Query(default=50, ge=1, le=200),
                           owner_id: UUID | None = Depends(authenticated_owner)) -> EnrollmentPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.INTEGRATIONS_READ)
    return await _call(_service(request).list_enrollments, owner, limit=limit)


@router.post("/enrollments", response_model=EnrollmentView, status_code=status.HTTP_201_CREATED, responses=_ERRORS)
async def create_enrollment(body: EnrollmentCreateRequest, request: Request,
                            owner_id: UUID | None = Depends(authenticated_owner)) -> EnrollmentView:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.INTEGRATIONS_MANAGE)
    return await _call(_service(request).create_enrollment, owner, body)


@router.get("/enrollments/{enrollment_id}", response_model=EnrollmentView, responses=_ERRORS)
async def get_enrollment(request: Request, enrollment_id: str = EnrollmentId,
                         owner_id: UUID | None = Depends(authenticated_owner)) -> EnrollmentView:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.INTEGRATIONS_READ)
    return await _call(_service(request).get_enrollment, owner, enrollment_id)


@router.post("/enrollments/{enrollment_id}/activate", response_model=EnrollmentView, responses=_GATED)
async def activate_enrollment(request: Request, enrollment_id: str = EnrollmentId,
                              approval_id: str | None = Query(default=None, pattern=r"^AR-[a-f0-9]{16}$",
                                                              description=APPROVAL_ID_HELP),
                              owner_id: UUID | None = Depends(authenticated_owner)) -> EnrollmentView:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.INTEGRATIONS_MANAGE)
    return await _call(_service(request).activate, owner, enrollment_id, approval_id)


@router.post("/enrollments/{enrollment_id}/revoke", response_model=EnrollmentView, responses=_ERRORS)
async def revoke_enrollment(request: Request, enrollment_id: str = EnrollmentId,
                            owner_id: UUID | None = Depends(authenticated_owner)) -> EnrollmentView:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.INTEGRATIONS_MANAGE)
    return await _call(_service(request).revoke, owner, enrollment_id)


@router.post("/enrollments/{enrollment_id}/actions", response_model=ActionResult, responses=_GATED)
async def run_action(body: ActionRequest, request: Request, enrollment_id: str = EnrollmentId,
                     owner_id: UUID | None = Depends(authenticated_owner)) -> ActionResult:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.INTEGRATIONS_ACT)
    return await _call(_service(request).act, owner, enrollment_id, body)
