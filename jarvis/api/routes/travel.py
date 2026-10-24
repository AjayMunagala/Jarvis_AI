"""Authenticated travel and decision planning API routes (v7.4).

Nothing here books, buys, pays or signs in anywhere, and no route calls an external travel service.
Suggestions use the app's configured provider; the demo provider's reply is labeled ``demo``.
"""

from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability
from jarvis.travel.models import (
    Decision, DecisionCreateRequest, DecisionPage, DecisionResult, DecisionUpdateRequest, ReminderRequest,
    ReminderResult, Segment, SegmentCreateRequest, SegmentPage, SegmentUpdateRequest, SuggestRequest,
    SuggestResponse, Trip, TripCreateRequest, TripPage, TripSummary, TripUpdateRequest,
)
from jarvis.travel.service import (
    TravelConflict, TravelInvalid, TravelModelOutputInvalid, TravelNotFound, TravelProviderTimeout,
    TravelProviderUnavailable, TravelService, TravelUnavailable,
)
from jarvis.travel.stores import TravelStorageError

router = APIRouter(prefix="/travel", tags=["travel"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 503)}
_WRITE_ERRORS = {**_ERRORS, 409: {"model": ErrorResponse}}
_MODEL_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 502, 503, 504)}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="A valid bearer credential is required.",
                            headers={"WWW-Authenticate": "Bearer"})
    return owner_id


def _service(request: Request) -> TravelService:
    service = getattr(request.app.state, "travel_service", None)
    if not isinstance(service, TravelService):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The travel service is unavailable.")
    return service


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, TravelNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, TravelInvalid):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, TravelConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, TravelModelOutputInvalid):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, (TravelProviderUnavailable, TravelUnavailable)):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, TravelProviderTimeout):
        return HTTPException(status_code=504, detail=str(exc))
    return HTTPException(status_code=503, detail="Travel storage is unavailable.")


_HANDLED = (TravelNotFound, TravelInvalid, TravelConflict, TravelModelOutputInvalid, TravelProviderUnavailable,
            TravelProviderTimeout, TravelUnavailable, TravelStorageError)


async def _call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return await run_in_threadpool(function, *args, **kwargs)
    except _HANDLED as exc:
        raise _translate(exc) from exc


def _authorize(request: Request, owner_id: UUID | None, capability: Capability) -> tuple[UUID, TravelService]:
    owner = _owner(owner_id)
    enforce_capability(request, owner, capability)
    return owner, _service(request)


# ============================================================= decisions

@router.post("/decisions", response_model=Decision, status_code=status.HTTP_201_CREATED, responses=_WRITE_ERRORS)
async def create_decision(request: Request, body: DecisionCreateRequest,
                          owner_id: UUID | None = Depends(authenticated_owner)) -> Decision:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_WRITE)
    return await _call(service.create_decision, owner, body)


@router.get("/decisions", response_model=DecisionPage, responses=_ERRORS)
async def list_decisions(request: Request, limit: int = Query(default=50, ge=1, le=200),
                         offset: int = Query(default=0, ge=0),
                         owner_id: UUID | None = Depends(authenticated_owner)) -> DecisionPage:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_READ)
    return await _call(service.list_decisions, owner, limit=limit, offset=offset)


@router.get("/decisions/{decision_id}", response_model=Decision, responses=_ERRORS)
async def get_decision(request: Request, decision_id: UUID,
                       owner_id: UUID | None = Depends(authenticated_owner)) -> Decision:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_READ)
    return await _call(service.get_decision, owner, decision_id)


@router.patch("/decisions/{decision_id}", response_model=Decision, responses=_ERRORS)
async def update_decision(request: Request, decision_id: UUID, body: DecisionUpdateRequest,
                          owner_id: UUID | None = Depends(authenticated_owner)) -> Decision:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_WRITE)
    return await _call(service.update_decision, owner, decision_id, body)


@router.delete("/decisions/{decision_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_decision(request: Request, decision_id: UUID,
                          owner_id: UUID | None = Depends(authenticated_owner)) -> Response:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_WRITE)
    await _call(service.delete_decision, owner, decision_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/decisions/{decision_id}/result", response_model=DecisionResult, responses=_ERRORS)
async def decision_result(request: Request, decision_id: UUID,
                          owner_id: UUID | None = Depends(authenticated_owner)) -> DecisionResult:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_READ)
    return await _call(service.decision_result, owner, decision_id)


# ============================================================= trips

@router.post("/trips", response_model=Trip, status_code=status.HTTP_201_CREATED, responses=_WRITE_ERRORS)
async def create_trip(request: Request, body: TripCreateRequest,
                      owner_id: UUID | None = Depends(authenticated_owner)) -> Trip:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_WRITE)
    return await _call(service.create_trip, owner, body)


@router.get("/trips", response_model=TripPage, responses=_ERRORS)
async def list_trips(request: Request, limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                     owner_id: UUID | None = Depends(authenticated_owner)) -> TripPage:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_READ)
    return await _call(service.list_trips, owner, limit=limit, offset=offset)


@router.get("/trips/{trip_id}", response_model=Trip, responses=_ERRORS)
async def get_trip(request: Request, trip_id: UUID, owner_id: UUID | None = Depends(authenticated_owner)) -> Trip:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_READ)
    return await _call(service.get_trip, owner, trip_id)


@router.patch("/trips/{trip_id}", response_model=Trip, responses=_ERRORS)
async def update_trip(request: Request, trip_id: UUID, body: TripUpdateRequest,
                      owner_id: UUID | None = Depends(authenticated_owner)) -> Trip:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_WRITE)
    return await _call(service.update_trip, owner, trip_id, body)


@router.delete("/trips/{trip_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_trip(request: Request, trip_id: UUID, owner_id: UUID | None = Depends(authenticated_owner)) -> Response:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_WRITE)
    await _call(service.delete_trip, owner, trip_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/trips/{trip_id}/summary", response_model=TripSummary, responses=_ERRORS)
async def trip_summary(request: Request, trip_id: UUID,
                       owner_id: UUID | None = Depends(authenticated_owner)) -> TripSummary:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_READ)
    return await _call(service.summary, owner, trip_id)


@router.post("/trips/{trip_id}/reminders", response_model=ReminderResult, responses=_ERRORS)
async def sync_reminders(request: Request, trip_id: UUID, body: ReminderRequest | None = None,
                         owner_id: UUID | None = Depends(authenticated_owner)) -> ReminderResult:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_WRITE)
    return await _call(service.sync_reminders, owner, trip_id, body)


@router.post("/trips/{trip_id}/suggest", response_model=SuggestResponse, responses=_MODEL_ERRORS)
async def suggest(request: Request, trip_id: UUID, body: SuggestRequest,
                  owner_id: UUID | None = Depends(authenticated_owner)) -> SuggestResponse:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_READ)
    trip = await _call(service.get_trip, owner, trip_id)
    segments = await _call(service.store.list_segments, owner, trip_id)
    try:
        return await service.suggest(getattr(request.app.state, "provider", None), trip, segments, body)
    except _HANDLED as exc:
        raise _translate(exc) from exc


# ============================================================= segments

@router.post("/trips/{trip_id}/segments", response_model=Segment, status_code=status.HTTP_201_CREATED,
             responses=_WRITE_ERRORS)
async def create_segment(request: Request, trip_id: UUID, body: SegmentCreateRequest,
                         owner_id: UUID | None = Depends(authenticated_owner)) -> Segment:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_WRITE)
    return await _call(service.create_segment, owner, trip_id, body)


@router.get("/trips/{trip_id}/segments", response_model=SegmentPage, responses=_ERRORS)
async def list_segments(request: Request, trip_id: UUID,
                        owner_id: UUID | None = Depends(authenticated_owner)) -> SegmentPage:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_READ)
    return await _call(service.list_segments, owner, trip_id)


@router.get("/trips/{trip_id}/segments/{segment_id}", response_model=Segment, responses=_ERRORS)
async def get_segment(request: Request, trip_id: UUID, segment_id: UUID,
                      owner_id: UUID | None = Depends(authenticated_owner)) -> Segment:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_READ)
    return await _call(service.get_segment, owner, trip_id, segment_id)


@router.patch("/trips/{trip_id}/segments/{segment_id}", response_model=Segment, responses=_ERRORS)
async def update_segment(request: Request, trip_id: UUID, segment_id: UUID, body: SegmentUpdateRequest,
                         owner_id: UUID | None = Depends(authenticated_owner)) -> Segment:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_WRITE)
    return await _call(service.update_segment, owner, trip_id, segment_id, body)


@router.delete("/trips/{trip_id}/segments/{segment_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_segment(request: Request, trip_id: UUID, segment_id: UUID,
                         owner_id: UUID | None = Depends(authenticated_owner)) -> Response:
    owner, service = _authorize(request, owner_id, Capability.TRAVEL_WRITE)
    await _call(service.delete_segment, owner, trip_id, segment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
