"""Authenticated proactive assistance and scheduling API routes (v3.5).

Provides scheduled job registration, background execution, anti-fatigue notification dispatching,
and notification acknowledgement. Owner-isolated and capability-checked.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.models.schemas import ErrorResponse
from jarvis.scheduler.models import (
    JobCreateRequest,
    JobRunResult,
    JobUpdateRequest,
    Notification,
    NotificationCreateRequest,
    NotificationPage,
    ScheduledJob,
    ScheduledJobPage,
)
from jarvis.scheduler.service import MaxConcurrencyError, SchedulerService
from jarvis.security.policy import Capability

router = APIRouter(prefix="/scheduler", tags=["scheduler"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 429, 503)}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return owner_id


def _service(request: Request) -> SchedulerService:
    service = getattr(request.app.state, "scheduler_service", None)
    if not isinstance(service, SchedulerService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The scheduler service is unavailable.",
        )
    return service


# ============================================================= Jobs

@router.post(
    "/jobs",
    response_model=ScheduledJob,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def create_job(
    request: Request,
    body: JobCreateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ScheduledJob:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.SCHEDULER_WRITE)
    return await run_in_threadpool(_service(request).create_job, owner, body)


@router.get("/jobs", response_model=ScheduledJobPage, responses=_ERRORS)
async def list_jobs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ScheduledJobPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.SCHEDULER_READ)
    return await run_in_threadpool(
        _service(request).list_jobs, owner, limit=limit, offset=offset
    )


@router.get("/jobs/{job_id}", response_model=ScheduledJob, responses=_ERRORS)
async def get_job(
    request: Request,
    job_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ScheduledJob:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.SCHEDULER_READ)
    job = await run_in_threadpool(_service(request).get_job, owner, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested scheduled job was not found.",
        )
    return job


@router.patch("/jobs/{job_id}", response_model=ScheduledJob, responses=_ERRORS)
async def update_job(
    request: Request,
    job_id: UUID,
    body: JobUpdateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ScheduledJob:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.SCHEDULER_WRITE)
    job = await run_in_threadpool(_service(request).update_job, owner, job_id, body)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested scheduled job was not found.",
        )
    return job


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_job(
    request: Request,
    job_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Response:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.SCHEDULER_WRITE)
    deleted = await run_in_threadpool(_service(request).delete_job, owner, job_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested scheduled job was not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/jobs/{job_id}/run", response_model=JobRunResult, responses=_ERRORS)
async def run_job(
    request: Request,
    job_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> JobRunResult:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.SCHEDULER_WRITE)
    try:
        return await run_in_threadpool(_service(request).execute_job, owner, job_id)
    except MaxConcurrencyError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


# ============================================================= Notifications

@router.post(
    "/notifications",
    response_model=Notification,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def send_notification(
    request: Request,
    body: NotificationCreateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Notification:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.SCHEDULER_WRITE)
    notif = await run_in_threadpool(
        _service(request).dispatch_notification, owner, body
    )
    if notif is None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Proactive notification for '{body.category}' suppressed by anti-fatigue policy.",
        )
    return notif


@router.get("/notifications", response_model=NotificationPage, responses=_ERRORS)
async def list_notifications(
    request: Request,
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> NotificationPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.SCHEDULER_READ)
    return await run_in_threadpool(
        _service(request).list_notifications,
        owner,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/notifications/{notification_id}",
    response_model=Notification,
    responses=_ERRORS,
)
async def get_notification(
    request: Request,
    notification_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Notification:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.SCHEDULER_READ)
    notif = await run_in_threadpool(
        _service(request).get_notification, owner, notification_id
    )
    if notif is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested notification was not found.",
        )
    return notif


@router.patch(
    "/notifications/{notification_id}/read",
    response_model=dict[str, bool],
    responses=_ERRORS,
)
async def mark_notification_read(
    request: Request,
    notification_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> dict[str, bool]:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.SCHEDULER_WRITE)
    marked = await run_in_threadpool(
        _service(request).mark_notification_read, owner, notification_id
    )
    if not marked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested notification was not found.",
        )
    return {"marked": True}
