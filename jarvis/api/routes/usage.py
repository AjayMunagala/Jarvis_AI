"""Authenticated model usage summary (v6.1)."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability
from jarvis.usage.service import UsageService, UsageSummary

router = APIRouter(tags=["usage"])


@router.get("/usage", response_model=UsageSummary, responses={code: {"model": ErrorResponse} for code in (401, 403, 503)})
async def usage(request: Request, days: int = Query(default=7, ge=1, le=90),
                owner_id: UUID | None = Depends(authenticated_owner)) -> UsageSummary:
    if owner_id is None:
        raise HTTPException(status_code=401, detail="A valid bearer credential is required.",
                            headers={"WWW-Authenticate": "Bearer"})
    enforce_capability(request, owner_id, Capability.STATUS_READ)
    service = getattr(request.app.state, "usage_service", None)
    if not isinstance(service, UsageService):
        raise HTTPException(status_code=503, detail="Usage tracking is unavailable.")
    try:
        return await run_in_threadpool(service.summary, days)
    except Exception as exc:  # noqa: BLE001 - sanitized
        raise HTTPException(status_code=503, detail="Usage storage is unavailable.") from exc
