"""Read-only repository intelligence and diagnostics endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.models.schemas import ErrorResponse, RepoDiagnosticRequest, RepoSearchRequest
from jarvis.repo.models import (
    ConfigDiscovery,
    DiagnosticReport,
    GitStatusSummary,
    RepositoryInventory,
    SearchSummary,
    SymbolInfo,
    TestDiscovery,
)
from jarvis.repo.service import RepoIntelligenceService
from jarvis.security.policy import Capability

router = APIRouter(prefix="/repo", tags=["repository-intelligence"])


def _repo_service(request: Request) -> RepoIntelligenceService:
    service = getattr(request.app.state, "repo_service", None)
    if not isinstance(service, RepoIntelligenceService):
        raise HTTPException(
            status_code=503,
            detail="Repository intelligence service is not configured.",
        )
    return service


def _require_owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(
            status_code=401,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return owner_id


@router.get(
    "/inventory",
    response_model=RepositoryInventory,
    responses={
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Operation not permitted"},
        503: {"model": ErrorResponse, "description": "Service unavailable"},
    },
)
async def get_inventory(
    request: Request,
    max_files: int = Query(default=5000, ge=1, le=10000),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> RepositoryInventory:
    """Bounded, read-only inventory of repository files, languages, and structure."""
    service = _repo_service(request)
    resolved_owner = _require_owner(owner_id)
    enforce_capability(request, resolved_owner, Capability.REPO_READ)
    return await run_in_threadpool(service.get_inventory, max_files=max_files)


@router.post(
    "/search",
    response_model=SearchSummary,
    responses={
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Operation not permitted"},
        422: {"model": ErrorResponse, "description": "Invalid search parameters"},
        503: {"model": ErrorResponse, "description": "Service unavailable"},
    },
)
async def search_repo(
    body: RepoSearchRequest,
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> SearchSummary:
    """Execute a bounded search over repository filenames or file contents."""
    service = _repo_service(request)
    resolved_owner = _require_owner(owner_id)
    enforce_capability(request, resolved_owner, Capability.REPO_READ)
    try:
        return await run_in_threadpool(
            service.search,
            body.query,
            search_type=body.search_type,
            is_regex=body.is_regex,
            case_sensitive=body.case_sensitive,
            path_filter=body.path_filter,
            max_results=body.max_results,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/symbols",
    response_model=list[SymbolInfo],
    responses={
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Operation not permitted"},
        503: {"model": ErrorResponse, "description": "Service unavailable"},
    },
)
async def search_symbols(
    request: Request,
    query: str = Query(min_length=1, max_length=200),
    path_filter: str | None = Query(default=None, max_length=255),
    max_results: int = Query(default=50, ge=1, le=200),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> list[SymbolInfo]:
    """Search for declared classes, functions, or route declarations."""
    service = _repo_service(request)
    resolved_owner = _require_owner(owner_id)
    enforce_capability(request, resolved_owner, Capability.REPO_READ)
    return await run_in_threadpool(
        service.find_symbols,
        query,
        path_filter=path_filter,
        max_results=max_results,
    )


@router.get(
    "/git",
    response_model=GitStatusSummary,
    responses={
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Operation not permitted"},
        503: {"model": ErrorResponse, "description": "Service unavailable"},
    },
)
async def get_git_status(
    request: Request,
    max_diff_entries: int = Query(default=50, ge=1, le=100),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> GitStatusSummary:
    """Safe, read-only inspection of Git branch, status, and diff summaries."""
    service = _repo_service(request)
    resolved_owner = _require_owner(owner_id)
    enforce_capability(request, resolved_owner, Capability.REPO_READ)
    return await run_in_threadpool(service.get_git_status, max_diff_entries=max_diff_entries)


@router.get(
    "/config",
    response_model=ConfigDiscovery,
    responses={
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Operation not permitted"},
        503: {"model": ErrorResponse, "description": "Service unavailable"},
    },
)
async def get_config(
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ConfigDiscovery:
    """Discover declared dependencies, build tools, frameworks, and configuration."""
    service = _repo_service(request)
    resolved_owner = _require_owner(owner_id)
    enforce_capability(request, resolved_owner, Capability.REPO_READ)
    return await run_in_threadpool(service.get_configuration)


@router.get(
    "/tests",
    response_model=TestDiscovery,
    responses={
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Operation not permitted"},
        503: {"model": ErrorResponse, "description": "Service unavailable"},
    },
)
async def get_test_discovery(
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> TestDiscovery:
    """Discover test frameworks, directories, files, and module mappings."""
    service = _repo_service(request)
    resolved_owner = _require_owner(owner_id)
    enforce_capability(request, resolved_owner, Capability.REPO_READ)
    return await run_in_threadpool(service.get_test_discovery)


@router.post(
    "/diagnostics",
    response_model=DiagnosticReport,
    responses={
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Operation not permitted"},
        503: {"model": ErrorResponse, "description": "Service unavailable"},
    },
)
async def diagnose_issue(
    body: RepoDiagnosticRequest,
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> DiagnosticReport:
    """Generate an evidence-based diagnostic report separating facts from hypotheses."""
    service = _repo_service(request)
    resolved_owner = _require_owner(owner_id)
    enforce_capability(request, resolved_owner, Capability.REPO_READ)
    return await run_in_threadpool(
        service.diagnose,
        body.query,
        target_files=body.target_files,
    )
