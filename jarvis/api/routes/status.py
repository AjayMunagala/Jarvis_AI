"""Authenticated per-subsystem status (v4.2).

``GET /status`` requires the owner bearer token and ``status.read``. The public
``/health`` and ``/ready`` endpoints are unchanged; this one is authenticated
because it names configured subsystems and their storage.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability
from jarvis.status.service import StatusReport, StatusService

router = APIRouter(tags=["status"])


@router.get("/status", response_model=StatusReport, responses={code: {"model": ErrorResponse} for code in (401, 403, 503)})
async def status(request: Request, owner_id: UUID | None = Depends(authenticated_owner)) -> StatusReport:
    if owner_id is None:
        raise HTTPException(status_code=401, detail="A valid bearer credential is required.",
                            headers={"WWW-Authenticate": "Bearer"})
    enforce_capability(request, owner_id, Capability.STATUS_READ)
    service = getattr(request.app.state, "status_service", None)
    if not isinstance(service, StatusService):
        raise HTTPException(status_code=503, detail="Status reporting is unavailable.")
    return await run_in_threadpool(service.report)
