"""Authenticated personal assistant API routes (v3.1).

Covers tasks, personal notes, reminders, and local calendar events.
Strictly owner-isolated and capability-checked.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.assistant.models import (
    CalendarEvent, CalendarEventCreateRequest, CalendarEventPage, CalendarEventUpdateRequest,
    Note, NoteCreateRequest, NotePage, NoteUpdateRequest,
    Reminder, ReminderCreateRequest, ReminderPage, ReminderStatus, ReminderUpdateRequest,
    Task, TaskCreateRequest, TaskPage, TaskPriority, TaskStatus, TaskUpdateRequest,
)
from jarvis.assistant.briefing import Conflict, DailyBriefing, build_briefing, conflicts_between
from jarvis.assistant.service import AssistantService
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability

router = APIRouter(prefix="/assistant", tags=["assistant"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 503)}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return owner_id


def _service(request: Request) -> AssistantService:
    service = getattr(request.app.state, "assistant_service", None)
    if not isinstance(service, AssistantService):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The assistant service is unavailable.")
    return service


# ============================================================= Tasks

@router.post("/tasks", response_model=Task, status_code=status.HTTP_201_CREATED, responses=_ERRORS)
async def create_task(
    request: Request,
    body: TaskCreateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Task:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_WRITE)
    return await run_in_threadpool(_service(request).create_task, owner, body)


@router.get("/tasks", response_model=TaskPage, responses=_ERRORS)
async def list_tasks(
    request: Request,
    status: TaskStatus | None = Query(default=None),
    priority: TaskPriority | None = Query(default=None),
    tag: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> TaskPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_READ)
    return await run_in_threadpool(
        _service(request).list_tasks, owner, status=status, priority=priority, tag=tag, limit=limit, offset=offset
    )


@router.get("/tasks/{task_id}", response_model=Task, responses=_ERRORS)
async def get_task(
    request: Request,
    task_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Task:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_READ)
    task = await run_in_threadpool(_service(request).get_task, owner, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The requested task was not found.")
    return task


@router.patch("/tasks/{task_id}", response_model=Task, responses=_ERRORS)
async def update_task(
    request: Request,
    task_id: UUID,
    body: TaskUpdateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Task:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_WRITE)
    task = await run_in_threadpool(_service(request).update_task, owner, task_id, body)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The requested task was not found.")
    return task


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_task(
    request: Request,
    task_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Response:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_WRITE)
    deleted = await run_in_threadpool(_service(request).delete_task, owner, task_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The requested task was not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================= Notes

@router.post("/notes", response_model=Note, status_code=status.HTTP_201_CREATED, responses=_ERRORS)
async def create_note(
    request: Request,
    body: NoteCreateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Note:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_WRITE)
    return await run_in_threadpool(_service(request).create_note, owner, body)


@router.get("/notes", response_model=NotePage, responses=_ERRORS)
async def list_notes(
    request: Request,
    query: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> NotePage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_READ)
    return await run_in_threadpool(
        _service(request).list_notes, owner, query=query, tag=tag, limit=limit, offset=offset
    )


@router.get("/notes/{note_id}", response_model=Note, responses=_ERRORS)
async def get_note(
    request: Request,
    note_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Note:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_READ)
    note = await run_in_threadpool(_service(request).get_note, owner, note_id)
    if note is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The requested note was not found.")
    return note


@router.patch("/notes/{note_id}", response_model=Note, responses=_ERRORS)
async def update_note(
    request: Request,
    note_id: UUID,
    body: NoteUpdateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Note:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_WRITE)
    note = await run_in_threadpool(_service(request).update_note, owner, note_id, body)
    if note is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The requested note was not found.")
    return note


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_note(
    request: Request,
    note_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Response:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_WRITE)
    deleted = await run_in_threadpool(_service(request).delete_note, owner, note_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The requested note was not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================= Reminders

@router.post("/reminders", response_model=Reminder, status_code=status.HTTP_201_CREATED, responses=_ERRORS)
async def create_reminder(
    request: Request,
    body: ReminderCreateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Reminder:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_WRITE)
    return await run_in_threadpool(_service(request).create_reminder, owner, body)


@router.get("/reminders", response_model=ReminderPage, responses=_ERRORS)
async def list_reminders(
    request: Request,
    status: ReminderStatus | None = Query(default=None),
    due_before: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ReminderPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_READ)
    return await run_in_threadpool(
        _service(request).list_reminders, owner, status=status, due_before=due_before, limit=limit, offset=offset
    )


@router.get("/reminders/{reminder_id}", response_model=Reminder, responses=_ERRORS)
async def get_reminder(
    request: Request,
    reminder_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Reminder:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_READ)
    reminder = await run_in_threadpool(_service(request).get_reminder, owner, reminder_id)
    if reminder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The requested reminder was not found.")
    return reminder


@router.patch("/reminders/{reminder_id}", response_model=Reminder, responses=_ERRORS)
async def update_reminder(
    request: Request,
    reminder_id: UUID,
    body: ReminderUpdateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Reminder:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_WRITE)
    reminder = await run_in_threadpool(_service(request).update_reminder, owner, reminder_id, body)
    if reminder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The requested reminder was not found.")
    return reminder


@router.delete("/reminders/{reminder_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_reminder(
    request: Request,
    reminder_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Response:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_WRITE)
    deleted = await run_in_threadpool(_service(request).delete_reminder, owner, reminder_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The requested reminder was not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================= Calendar Events

@router.post("/calendar/events", response_model=CalendarEvent, status_code=status.HTTP_201_CREATED, responses=_ERRORS)
async def create_calendar_event(
    request: Request,
    body: CalendarEventCreateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> CalendarEvent:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_WRITE)
    return await run_in_threadpool(_service(request).create_calendar_event, owner, body)


@router.get("/calendar/events", response_model=CalendarEventPage, responses=_ERRORS)
async def list_calendar_events(
    request: Request,
    start_range: datetime = Query(...),
    end_range: datetime = Query(...),
    limit: int = Query(default=100, ge=1, le=500),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> CalendarEventPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_READ)
    if end_range < start_range:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="end_range must be greater than or equal to start_range.")
    return await run_in_threadpool(
        _service(request).list_calendar_events, owner, start_range=start_range, end_range=end_range, limit=limit
    )


@router.get("/calendar/events/{event_id}", response_model=CalendarEvent, responses=_ERRORS)
async def get_calendar_event(
    request: Request,
    event_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> CalendarEvent:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_READ)
    event = await run_in_threadpool(_service(request).get_calendar_event, owner, event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The requested calendar event was not found.")
    return event


@router.patch("/calendar/events/{event_id}", response_model=CalendarEvent, responses=_ERRORS)
async def update_calendar_event(
    request: Request,
    event_id: UUID,
    body: CalendarEventUpdateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> CalendarEvent:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_WRITE)
    event = await run_in_threadpool(_service(request).update_calendar_event, owner, event_id, body.model_dump(exclude_unset=True))
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The requested calendar event was not found.")
    return event


@router.delete("/calendar/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_calendar_event(
    request: Request,
    event_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Response:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_WRITE)
    deleted = await run_in_threadpool(_service(request).delete_calendar_event, owner, event_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="The requested calendar event was not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================= Briefing and conflicts (v6.3)

@router.get("/briefing", response_model=DailyBriefing, responses=_ERRORS)
async def daily_briefing(
    request: Request,
    utc_offset_minutes: int | None = Query(default=None, ge=-720, le=840),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> DailyBriefing:
    """Today's overdue and due tasks, events, next-24-hour reminders and 7-day conflicts, from stored records only."""
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_READ)
    offset = utc_offset_minutes if utc_offset_minutes is not None else request.app.state.settings.scheduler_utc_offset_minutes
    return await run_in_threadpool(build_briefing, _service(request), owner, utc_offset_minutes=offset)


@router.get("/calendar/conflicts", response_model=tuple[Conflict, ...], responses=_ERRORS)
async def calendar_conflicts(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> tuple[Conflict, ...]:
    """Pairs of overlapping calendar events from now until ``days`` ahead."""
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.ASSISTANT_READ)
    from datetime import timedelta, timezone

    start = datetime.now(timezone.utc)
    return await run_in_threadpool(conflicts_between, _service(request), owner, start, start + timedelta(days=days))

