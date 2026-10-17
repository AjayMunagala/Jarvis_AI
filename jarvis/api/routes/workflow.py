"""Authenticated workflow orchestration API routes (v3.2).

Provides DAG workflow definitions, runs, step inspection, and cancellation.
Owner-isolated and capability-checked.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability
from jarvis.workflow.models import (
    WorkflowDefinition,
    WorkflowDefinitionCreateRequest,
    WorkflowDefinitionPage,
    WorkflowRunDetail,
    WorkflowRunPage,
    WorkflowRunTriggerRequest,
    WorkflowStatus,
)
from jarvis.workflow.service import WorkflowExecutionError, WorkflowService

router = APIRouter(prefix="/workflows", tags=["workflows"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 503)}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return owner_id


def _service(request: Request) -> WorkflowService:
    service = getattr(request.app.state, "workflow_service", None)
    if not isinstance(service, WorkflowService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The workflow service is unavailable.",
        )
    return service


# ============================================================= Definitions

@router.post(
    "/definitions",
    response_model=WorkflowDefinition,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def create_workflow_definition(
    request: Request,
    body: WorkflowDefinitionCreateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> WorkflowDefinition:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.WORKFLOW_WRITE)
    return await run_in_threadpool(_service(request).create_definition, owner, body)


@router.get("/definitions", response_model=WorkflowDefinitionPage, responses=_ERRORS)
async def list_workflow_definitions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> WorkflowDefinitionPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.WORKFLOW_READ)
    return await run_in_threadpool(
        _service(request).list_definitions, owner, limit=limit, offset=offset
    )


@router.get(
    "/definitions/{workflow_id}",
    response_model=WorkflowDefinition,
    responses=_ERRORS,
)
async def get_workflow_definition(
    request: Request,
    workflow_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> WorkflowDefinition:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.WORKFLOW_READ)
    definition = await run_in_threadpool(
        _service(request).get_definition, owner, workflow_id
    )
    if definition is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested workflow definition was not found.",
        )
    return definition


@router.delete(
    "/definitions/{workflow_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_ERRORS,
)
async def delete_workflow_definition(
    request: Request,
    workflow_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Response:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.WORKFLOW_WRITE)
    deleted = await run_in_threadpool(
        _service(request).delete_definition, owner, workflow_id
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested workflow definition was not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================= Runs

@router.post(
    "/definitions/{workflow_id}/run",
    response_model=WorkflowRunDetail,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def trigger_workflow_run(
    request: Request,
    workflow_id: UUID,
    body: WorkflowRunTriggerRequest = WorkflowRunTriggerRequest(),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> WorkflowRunDetail:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.WORKFLOW_WRITE)
    try:
        return await run_in_threadpool(
            _service(request).trigger_run, owner, workflow_id, body.inputs
        )
    except WorkflowExecutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.get("/runs", response_model=WorkflowRunPage, responses=_ERRORS)
async def list_workflow_runs(
    request: Request,
    workflow_id: UUID | None = Query(default=None),
    status: WorkflowStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> WorkflowRunPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.WORKFLOW_READ)
    return await run_in_threadpool(
        _service(request).list_runs,
        owner,
        workflow_id=workflow_id,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.get("/runs/{run_id}", response_model=WorkflowRunDetail, responses=_ERRORS)
async def get_workflow_run(
    request: Request,
    run_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> WorkflowRunDetail:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.WORKFLOW_READ)
    detail = await run_in_threadpool(_service(request).get_run, owner, run_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested workflow run was not found.",
        )
    return detail


@router.post(
    "/runs/{run_id}/cancel",
    response_model=WorkflowRunDetail,
    responses=_ERRORS,
)
async def cancel_workflow_run(
    request: Request,
    run_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> WorkflowRunDetail:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.WORKFLOW_WRITE)
    detail = await run_in_threadpool(_service(request).cancel_run, owner, run_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested workflow run was not found.",
        )
    return detail


@router.post(
    "/runs/{run_id}/compensate",
    response_model=dict[str, Any],
    responses=_ERRORS,
)
async def compensate_workflow_run(
    request: Request,
    run_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> dict[str, Any]:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.WORKFLOW_WRITE)
    try:
        return await run_in_threadpool(_service(request).compensate_run, owner, run_id)
    except WorkflowExecutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.get(
    "/recovery/dead-letters",
    response_model=list[dict[str, Any]],
    responses=_ERRORS,
)
async def list_dead_letters(
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> list[dict[str, Any]]:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.WORKFLOW_READ)
    return await run_in_threadpool(_service(request).get_dead_letters, owner)
