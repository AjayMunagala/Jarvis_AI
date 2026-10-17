"""Authenticated capability registry and read-only tool runtime endpoints (v2.7).

- ``GET /capabilities`` (``tools.read``): registered tools with risk, permissions,
  schemas, limits, and per-request availability.
- ``POST /tools/calls`` (``tools.invoke``): invoke one registered LOW-risk
  read-only tool. Runtime outcomes (succeeded, denied, invalid_input,
  unavailable, timed_out, failed, approval_required) are returned in the
  ``ToolResult`` body with HTTP 200; the caller must read ``status``.
- ``GET /tools/audit`` (``tools.read``): recent metadata-only audit events
  (in-memory, non-durable in v2.7).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability
from jarvis.tools.handlers import ToolContext
from jarvis.tools.models import AuditPage, CapabilitiesResponse, ToolCallRequest, ToolResult
from jarvis.tools.runtime import ToolRuntime

router = APIRouter(tags=["tools"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 413, 422, 503)}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(status_code=401, detail="A valid bearer credential is required.",
                            headers={"WWW-Authenticate": "Bearer"})
    return owner_id


def _runtime(request: Request) -> ToolRuntime:
    runtime = getattr(request.app.state, "tool_runtime", None)
    if not isinstance(runtime, ToolRuntime):
        raise HTTPException(status_code=503, detail="The tool runtime is unavailable.")
    return runtime


def _context(request: Request, owner_id: UUID) -> ToolContext:
    state = request.app.state
    return ToolContext(
        owner_id=owner_id, settings=state.settings,
        memory_repository=getattr(state, "memory_repository", None),
        repo_service=getattr(state, "repo_service", None),
        verification_service=getattr(state, "verification_service", None),
        browser_service=getattr(state, "browser_service", None),
    )


@router.get("/capabilities", response_model=CapabilitiesResponse, responses=_ERRORS)
async def list_capabilities(request: Request, owner_id: UUID | None = Depends(authenticated_owner)) -> CapabilitiesResponse:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.TOOLS_READ)
    runtime = _runtime(request)
    return await run_in_threadpool(runtime.describe, owner, _context(request, owner),
                                   policy=request.app.state.authorization_policy)


@router.post("/tools/calls", response_model=ToolResult, responses=_ERRORS)
async def call_tool(body: ToolCallRequest, request: Request,
                    owner_id: UUID | None = Depends(authenticated_owner)) -> ToolResult:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.TOOLS_INVOKE)
    runtime = _runtime(request)
    return await run_in_threadpool(
        runtime.invoke, owner, body.tool_id, body.arguments, _context(request, owner),
        correlation_id=request.headers.get("x-request-id"), policy=request.app.state.authorization_policy,
    )


@router.get("/tools/audit", response_model=AuditPage, responses=_ERRORS)
async def tool_audit(request: Request, limit: int = Query(default=50, ge=1, le=200),
                     owner_id: UUID | None = Depends(authenticated_owner)) -> AuditPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.TOOLS_READ)
    return _runtime(request).audit_page(limit)
