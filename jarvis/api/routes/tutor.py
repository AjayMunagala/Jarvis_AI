"""Authenticated tutor API routes (v6.5).

Exercises are returned without their answers and explanations until the owner submits an attempt.
Explanations and exercise generation use the app's configured provider; the demo provider's
explanation is labeled ``demo`` and generation refuses it (409).
"""

from datetime import date
from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability
from jarvis.tutor.models import (
    AttemptRequest, AttemptResult, ExerciseCreateRequest, ExerciseView, ExerciseViewPage, ExplainRequest,
    ExplainResponse, GeneratedExercises, GenerateExercisesRequest, GoalCreateRequest, GoalProgress, GoalStatus,
    GoalUpdateRequest, LearningGoal, LearningGoalPage, ReviewQueue, StudyPlan,
)
from jarvis.tutor.service import (
    TutorConflict, TutorInvalid, TutorModelOutputInvalid, TutorNotFound, TutorProviderTimeout,
    TutorProviderUnavailable, TutorService,
)
from jarvis.tutor.stores import TutorStorageError

router = APIRouter(prefix="/tutor", tags=["tutor"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 503)}
_MODEL_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422, 502, 503, 504)}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return owner_id


def _service(request: Request) -> TutorService:
    service = getattr(request.app.state, "tutor_service", None)
    if not isinstance(service, TutorService):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The tutor service is unavailable.")
    return service


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, TutorNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, TutorInvalid):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, TutorConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, TutorModelOutputInvalid):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, TutorProviderUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, TutorProviderTimeout):
        return HTTPException(status_code=504, detail=str(exc))
    return HTTPException(status_code=503, detail="Tutor storage is unavailable.")


_HANDLED = (TutorNotFound, TutorInvalid, TutorConflict, TutorModelOutputInvalid, TutorProviderUnavailable,
            TutorProviderTimeout, TutorStorageError)


async def _call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return await run_in_threadpool(function, *args, **kwargs)
    except _HANDLED as exc:
        raise _translate(exc) from exc


def _authorize(request: Request, owner_id: UUID | None, capability: Capability) -> tuple[UUID, TutorService]:
    owner = _owner(owner_id)
    enforce_capability(request, owner, capability)
    return owner, _service(request)


# ============================================================= goals

@router.post("/goals", response_model=LearningGoal, status_code=status.HTTP_201_CREATED,
             responses={**_ERRORS, 409: {"model": ErrorResponse}})
async def create_goal(request: Request, body: GoalCreateRequest,
                      owner_id: UUID | None = Depends(authenticated_owner)) -> LearningGoal:
    owner, service = _authorize(request, owner_id, Capability.TUTOR_WRITE)
    return await _call(service.create_goal, owner, body)


@router.get("/goals", response_model=LearningGoalPage, responses=_ERRORS)
async def list_goals(request: Request, status_filter: GoalStatus | None = Query(default=None, alias="status"),
                     limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
                     owner_id: UUID | None = Depends(authenticated_owner)) -> LearningGoalPage:
    owner, service = _authorize(request, owner_id, Capability.TUTOR_READ)
    return await _call(service.list_goals, owner, status=status_filter, limit=limit, offset=offset)


@router.get("/goals/{goal_id}", response_model=LearningGoal, responses=_ERRORS)
async def get_goal(request: Request, goal_id: UUID,
                   owner_id: UUID | None = Depends(authenticated_owner)) -> LearningGoal:
    owner, service = _authorize(request, owner_id, Capability.TUTOR_READ)
    return await _call(service.get_goal, owner, goal_id)


@router.patch("/goals/{goal_id}", response_model=LearningGoal, responses=_ERRORS)
async def update_goal(request: Request, goal_id: UUID, body: GoalUpdateRequest,
                      owner_id: UUID | None = Depends(authenticated_owner)) -> LearningGoal:
    owner, service = _authorize(request, owner_id, Capability.TUTOR_WRITE)
    return await _call(service.update_goal, owner, goal_id, body)


@router.delete("/goals/{goal_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_goal(request: Request, goal_id: UUID,
                      owner_id: UUID | None = Depends(authenticated_owner)) -> Response:
    owner, service = _authorize(request, owner_id, Capability.TUTOR_WRITE)
    await _call(service.delete_goal, owner, goal_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/goals/{goal_id}/plan", response_model=StudyPlan, responses=_ERRORS)
async def study_plan(request: Request, goal_id: UUID,
                     start: date | None = Query(default=None, description="Start date (YYYY-MM-DD); default today UTC."),
                     owner_id: UUID | None = Depends(authenticated_owner)) -> StudyPlan:
    owner, service = _authorize(request, owner_id, Capability.TUTOR_READ)
    return await _call(service.study_plan, owner, goal_id, start)


# ============================================================= exercises

@router.post("/goals/{goal_id}/exercises", response_model=ExerciseView, status_code=status.HTTP_201_CREATED,
             responses={**_ERRORS, 409: {"model": ErrorResponse}})
async def create_exercise(request: Request, goal_id: UUID, body: ExerciseCreateRequest,
                          owner_id: UUID | None = Depends(authenticated_owner)) -> ExerciseView:
    owner, service = _authorize(request, owner_id, Capability.TUTOR_WRITE)
    return await _call(service.create_exercise, owner, goal_id, body)


@router.get("/goals/{goal_id}/exercises", response_model=ExerciseViewPage, responses=_ERRORS)
async def list_exercises(request: Request, goal_id: UUID, limit: int = Query(default=50, ge=1, le=200),
                         offset: int = Query(default=0, ge=0),
                         owner_id: UUID | None = Depends(authenticated_owner)) -> ExerciseViewPage:
    owner, service = _authorize(request, owner_id, Capability.TUTOR_READ)
    return await _call(service.list_exercises, owner, goal_id, limit=limit, offset=offset)


@router.post("/goals/{goal_id}/exercises/generate", response_model=GeneratedExercises,
             status_code=status.HTTP_201_CREATED, responses=_MODEL_ERRORS)
async def generate_exercises(request: Request, goal_id: UUID, body: GenerateExercisesRequest,
                             owner_id: UUID | None = Depends(authenticated_owner)) -> GeneratedExercises:
    owner, service = _authorize(request, owner_id, Capability.TUTOR_WRITE)
    goal = await _call(service.get_goal, owner, goal_id)
    try:
        return await service.generate_exercises(owner, getattr(request.app.state, "provider", None), goal, body)
    except _HANDLED as exc:
        raise _translate(exc) from exc


@router.get("/goals/{goal_id}/review", response_model=ReviewQueue, responses=_ERRORS)
async def review_queue(request: Request, goal_id: UUID, limit: int = Query(default=20, ge=1, le=50),
                       owner_id: UUID | None = Depends(authenticated_owner)) -> ReviewQueue:
    owner, service = _authorize(request, owner_id, Capability.TUTOR_READ)
    return await _call(service.review_queue, owner, goal_id, limit=limit)


@router.get("/goals/{goal_id}/progress", response_model=GoalProgress, responses=_ERRORS)
async def progress(request: Request, goal_id: UUID,
                   owner_id: UUID | None = Depends(authenticated_owner)) -> GoalProgress:
    owner, service = _authorize(request, owner_id, Capability.TUTOR_READ)
    return await _call(service.progress, owner, goal_id)


@router.get("/exercises/{exercise_id}", response_model=ExerciseView, responses=_ERRORS)
async def get_exercise(request: Request, exercise_id: UUID,
                       owner_id: UUID | None = Depends(authenticated_owner)) -> ExerciseView:
    owner, service = _authorize(request, owner_id, Capability.TUTOR_READ)
    return await _call(service.get_exercise, owner, exercise_id)


@router.delete("/exercises/{exercise_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def delete_exercise(request: Request, exercise_id: UUID,
                          owner_id: UUID | None = Depends(authenticated_owner)) -> Response:
    owner, service = _authorize(request, owner_id, Capability.TUTOR_WRITE)
    await _call(service.delete_exercise, owner, exercise_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/exercises/{exercise_id}/attempts", response_model=AttemptResult, responses=_ERRORS)
async def record_attempt(request: Request, exercise_id: UUID, body: AttemptRequest,
                         owner_id: UUID | None = Depends(authenticated_owner)) -> AttemptResult:
    owner, service = _authorize(request, owner_id, Capability.TUTOR_WRITE)
    return await _call(service.record_attempt, owner, exercise_id, body)


# ============================================================= explain

@router.post("/explain", response_model=ExplainResponse, responses=_MODEL_ERRORS)
async def explain(request: Request, body: ExplainRequest,
                  owner_id: UUID | None = Depends(authenticated_owner)) -> ExplainResponse:
    owner, service = _authorize(request, owner_id, Capability.TUTOR_READ)
    try:
        return await service.explain(getattr(request.app.state, "provider", None), body)
    except _HANDLED as exc:
        raise _translate(exc) from exc
