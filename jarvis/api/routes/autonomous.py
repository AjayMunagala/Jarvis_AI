"""Authenticated Autonomous AI Operating System API routes (v4.0).

Exposes the capstone autonomous command execution endpoint, system overview, and command history.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.autonomous.coordinator import AutonomousCoordinator
from jarvis.autonomous.models import (
    AutonomousCommandRequest,
    AutonomousCommandResult,
    SystemOverview,
)
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability

router = APIRouter(prefix="/autonomous", tags=["autonomous"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 503)}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return owner_id


def _coordinator(request: Request) -> AutonomousCoordinator:
    coord = getattr(request.app.state, "autonomous_coordinator", None)
    if not isinstance(coord, AutonomousCoordinator):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The autonomous coordinator is unavailable.",
        )
    return coord


@router.post(
    "/execute",
    response_model=AutonomousCommandResult,
    status_code=status.HTTP_200_OK,
    responses=_ERRORS,
)
async def execute_autonomous_command(
    request: Request,
    body: AutonomousCommandRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> AutonomousCommandResult:
    """Dispatches a natural-language command across JARVIS subsystems."""
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.AUTONOMOUS_EXECUTE)
    return await run_in_threadpool(_coordinator(request).execute_command, owner, body)


@router.get(
    "/overview",
    response_model=SystemOverview,
    status_code=status.HTTP_200_OK,
    responses=_ERRORS,
)
async def get_system_overview(
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> SystemOverview:
    """Returns the operational status and telemetry of all 8 modular subsystems."""
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.AUTONOMOUS_READ)
    return await run_in_threadpool(_coordinator(request).get_system_overview, owner)


@router.get(
    "/history",
    response_model=list[AutonomousCommandResult],
    status_code=status.HTTP_200_OK,
    responses=_ERRORS,
)
async def get_command_history(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> list[AutonomousCommandResult]:
    """Returns the history of executed autonomous commands for the authenticated owner."""
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.AUTONOMOUS_READ)
    return await run_in_threadpool(_coordinator(request).get_history, owner, limit=limit)
