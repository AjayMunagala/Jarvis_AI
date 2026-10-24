"""Authenticated event source API routes (v7.3).

Owner-scoped. Reading needs ``scheduler.read`` and changing or polling needs ``scheduler.write``: sources are part
of the proactive notification subsystem, so they reuse its capabilities. Item text is untrusted plain text.
"""

from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability
from jarvis.sources.models import (
    EventSource, EventSourcePage, PollResult, SourceCreateRequest, SourceItemPage, SourceUpdateRequest,
)
from jarvis.sources.service import SourceConflict, SourceInvalid, SourceNotFound, SourcesService
from jarvis.sources.stores import SourceStorageError

router = APIRouter(prefix="/sources", tags=["sources"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 503)}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="A valid bearer credential is required.",
                            headers={"WWW-Authenticate": "Bearer"})
    return owner_id


def _authorize(request: Request, owner_id: UUID | None, capability: Capability) -> tuple[UUID, SourcesService]:
    owner = _owner(owner_id)
    enforce_capability(request, owner, capability)
    service = getattr(request.app.state, "sources_service", None)
    if not isinstance(service, SourcesService):
        raise HTTPException(status_code=503, detail="The sources service is unavailable.")
    return owner, service


async def _call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return await run_in_threadpool(function, *args, **kwargs)
    except SourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SourceInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SourceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SourceStorageError as exc:
        raise HTTPException(status_code=503, detail="Source storage is unavailable.") from exc


@router.post("", response_model=EventSource, status_code=status.HTTP_201_CREATED, responses=_ERRORS)
async def create_source(request: Request, body: SourceCreateRequest,
                        owner_id: UUID | None = Depends(authenticated_owner)) -> EventSource:
    owner, service = _authorize(request, owner_id, Capability.SCHEDULER_WRITE)
    return await _call(service.create_source, owner, body)


@router.get("", response_model=EventSourcePage, responses=_ERRORS)
async def list_sources(request: Request, limit: int = Query(default=50, ge=1, le=50),
                       offset: int = Query(default=0, ge=0),
                       owner_id: UUID | None = Depends(authenticated_owner)) -> EventSourcePage:
    owner, service = _authorize(request, owner_id, Capability.SCHEDULER_READ)
    return await _call(service.list_sources, owner, limit=limit, offset=offset)


@router.get("/{source_id}", response_model=EventSource, responses=_ERRORS)
async def get_source(request: Request, source_id: UUID,
                     owner_id: UUID | None = Depends(authenticated_owner)) -> EventSource:
    owner, service = _authorize(request, owner_id, Capability.SCHEDULER_READ)
    return await _call(service.get_source, owner, source_id)


@router.patch("/{source_id}", response_model=EventSource, responses=_ERRORS)
async def update_source(request: Request, source_id: UUID, body: SourceUpdateRequest,
                        owner_id: UUID | None = Depends(authenticated_owner)) -> EventSource:
    owner, service = _authorize(request, owner_id, Capability.SCHEDULER_WRITE)
    return await _call(service.update_source, owner, source_id, body)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_source(request: Request, source_id: UUID,
                        owner_id: UUID | None = Depends(authenticated_owner)) -> Response:
    owner, service = _authorize(request, owner_id, Capability.SCHEDULER_WRITE)
    await _call(service.delete_source, owner, source_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{source_id}/poll", response_model=PollResult, responses=_ERRORS)
async def poll_source(request: Request, source_id: UUID,
                      owner_id: UUID | None = Depends(authenticated_owner)) -> PollResult:
    owner, service = _authorize(request, owner_id, Capability.SCHEDULER_WRITE)
    return await _call(service.poll_now, owner, source_id)


@router.get("/{source_id}/items", response_model=SourceItemPage, responses=_ERRORS)
async def list_items(request: Request, source_id: UUID, limit: int = Query(default=50, ge=1, le=50),
                     owner_id: UUID | None = Depends(authenticated_owner)) -> SourceItemPage:
    owner, service = _authorize(request, owner_id, Capability.SCHEDULER_READ)
    return await _call(service.list_items, owner, source_id, limit=limit)
