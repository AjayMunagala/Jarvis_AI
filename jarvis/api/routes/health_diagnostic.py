"""Authenticated health diagnostic and security quarantine API routes (v3.6).

Provides system telemetry snapshots, diagnostic runs with secret scanning,
and approval-gated quarantine/restore operations.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.health.models import (
    DiagnosticReport,
    HealthSnapshot,
    HealthSnapshotPage,
    QuarantineRequest,
    QuarantinedFile,
    QuarantinedFilePage,
)
from jarvis.health.service import HealthService
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability

router = APIRouter(prefix="/health", tags=["health-diagnostic"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 503)}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return owner_id


def _service(request: Request) -> HealthService:
    service = getattr(request.app.state, "health_service", None)
    if not isinstance(service, HealthService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The health service is unavailable.",
        )
    return service


# ============================================================= Diagnostics

@router.get("/diagnostic/snapshots", response_model=HealthSnapshotPage, responses=_ERRORS)
async def list_snapshots(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> HealthSnapshotPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.HEALTH_READ)
    return await run_in_threadpool(
        _service(request).list_snapshots, owner, limit=limit, offset=offset
    )


@router.post(
    "/diagnostic/snapshots",
    response_model=HealthSnapshot,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def capture_snapshot(
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> HealthSnapshot:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.HEALTH_DIAGNOSTIC_RUN)
    return await run_in_threadpool(_service(request).capture_snapshot, owner)


@router.post("/diagnostic/run", response_model=DiagnosticReport, responses=_ERRORS)
async def run_diagnostics(
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> DiagnosticReport:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.HEALTH_DIAGNOSTIC_RUN)
    return await run_in_threadpool(_service(request).run_diagnostics, owner)


# ============================================================= Quarantine

@router.get("/security/quarantined", response_model=QuarantinedFilePage, responses=_ERRORS)
async def list_quarantined_files(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> QuarantinedFilePage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.HEALTH_READ)
    return await run_in_threadpool(
        _service(request).list_quarantined_files, owner, limit=limit, offset=offset
    )


@router.post(
    "/security/quarantine",
    response_model=QuarantinedFile,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def quarantine_file(
    request: Request,
    body: QuarantineRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> QuarantinedFile:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.SECURITY_QUARANTINE_FILE)
    try:
        return await run_in_threadpool(
            _service(request).quarantine_file, owner, body, approved_by=owner
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.post(
    "/security/quarantined/{record_id}/restore",
    response_model=QuarantinedFile,
    responses=_ERRORS,
)
async def restore_quarantined_file(
    request: Request,
    record_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> QuarantinedFile:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.SECURITY_QUARANTINE_FILE)
    try:
        return await run_in_threadpool(
            _service(request).restore_quarantined_file, owner, record_id
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
