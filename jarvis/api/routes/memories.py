"""Explicit memory management and deterministic lexical search endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner
from jarvis.memory.base import MemoryPersistenceError, MemoryRepository
from jarvis.memory.models import (
    MemoryCreate, MemoryPage, MemoryRecord, MemorySearchRequest, MemoryUpdate,
)
from jarvis.models.schemas import ErrorResponse

router = APIRouter(prefix="/memories", tags=["memories"])


def _repository(request: Request) -> MemoryRepository:
    repository = request.app.state.memory_repository
    if repository is None:
        raise HTTPException(
            status_code=503,
            detail="Memory persistence is not enabled.",
        )
    return repository


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="Memory storage is unavailable.")


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        # Persistence is disabled; no injected repository may bypass this policy.
        raise _unavailable()
    return owner_id


@router.post(
    "",
    response_model=MemoryRecord,
    status_code=status.HTTP_201_CREATED,
    responses={503: {"model": ErrorResponse, "description": "Memory persistence unavailable"}},
)
async def create_memory(
    body: MemoryCreate, request: Request, owner_id: UUID | None = Depends(authenticated_owner)
) -> MemoryRecord:
    repository = _repository(request)
    owner_id = _owner(owner_id)
    try:
        return await run_in_threadpool(
            repository.create, owner_id, body.content, body.category, body.source_reference
        )
    except MemoryPersistenceError as exc:
        raise _unavailable() from exc


@router.post("/search", response_model=MemoryPage)
async def search_memories(
    body: MemorySearchRequest,
    request: Request,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> MemoryPage:
    repository = _repository(request)
    owner_id = _owner(owner_id)
    try:
        return await run_in_threadpool(
            repository.search, owner_id, body.query, limit=body.limit, offset=body.offset
        )
    except MemoryPersistenceError as exc:
        raise _unavailable() from exc


@router.get("", response_model=MemoryPage)
async def list_memories(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000000),
    category: str | None = Query(default=None, min_length=1, max_length=80),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> MemoryPage:
    if category is not None and not category.strip():
        raise HTTPException(status_code=422, detail="Category must not be blank.")
    repository = _repository(request)
    owner_id = _owner(owner_id)
    try:
        return await run_in_threadpool(repository.list, owner_id, limit=limit, offset=offset, category=category)
    except MemoryPersistenceError as exc:
        raise _unavailable() from exc


@router.get(
    "/{memory_id}",
    response_model=MemoryRecord,
    responses={
        404: {"model": ErrorResponse, "description": "Memory not found"},
        503: {"model": ErrorResponse, "description": "Memory persistence unavailable"},
    },
)
async def get_memory(
    memory_id: UUID, request: Request, owner_id: UUID | None = Depends(authenticated_owner)
) -> MemoryRecord:
    repository = _repository(request)
    owner_id = _owner(owner_id)
    try:
        memory = await run_in_threadpool(repository.get, owner_id, memory_id)
    except MemoryPersistenceError as exc:
        raise _unavailable() from exc
    if memory is None:
        raise HTTPException(status_code=404, detail="The requested memory was not found.")
    return memory


@router.patch(
    "/{memory_id}",
    response_model=MemoryRecord,
    responses={
        404: {"model": ErrorResponse, "description": "Memory not found"},
        503: {"model": ErrorResponse, "description": "Memory persistence unavailable"},
    },
)
async def update_memory(
    memory_id: UUID, body: MemoryUpdate, request: Request, owner_id: UUID | None = Depends(authenticated_owner)
) -> MemoryRecord:
    repository = _repository(request)
    owner_id = _owner(owner_id)
    try:
        memory = await run_in_threadpool(repository.update, owner_id, memory_id, body)
    except MemoryPersistenceError as exc:
        raise _unavailable() from exc
    if memory is None:
        raise HTTPException(status_code=404, detail="The requested memory was not found.")
    return memory


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"model": ErrorResponse, "description": "Memory not found"},
        503: {"model": ErrorResponse, "description": "Memory persistence unavailable"},
    },
)
async def delete_memory(
    memory_id: UUID, request: Request, owner_id: UUID | None = Depends(authenticated_owner)
) -> Response:
    repository = _repository(request)
    owner_id = _owner(owner_id)
    try:
        deleted = await run_in_threadpool(repository.delete, owner_id, memory_id)
    except MemoryPersistenceError as exc:
        raise _unavailable() from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="The requested memory was not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
