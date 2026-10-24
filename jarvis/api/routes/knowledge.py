"""Authenticated knowledge intelligence API routes (v3.3).

Provides document ingestion, chunking, hybrid search (RRF), and context budgeting.
Owner-isolated and capability-checked.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.knowledge.models import (
    ContextBudgetAllocation,
    ContextBudgetRequest,
    KnowledgeDocumentCreateRequest,
    KnowledgeDocumentDetail,
    KnowledgeDocumentPage,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)
from jarvis.knowledge.service import KnowledgeService
from jarvis.models.schemas import ErrorResponse
from jarvis.security.policy import Capability

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 503)}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return owner_id


def _service(request: Request) -> KnowledgeService:
    service = getattr(request.app.state, "knowledge_service", None)
    if not isinstance(service, KnowledgeService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The knowledge service is unavailable.",
        )
    return service


# ============================================================= Documents

@router.post(
    "/documents",
    response_model=KnowledgeDocumentDetail,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def ingest_document(
    request: Request,
    body: KnowledgeDocumentCreateRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> KnowledgeDocumentDetail:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.KNOWLEDGE_WRITE)
    return await run_in_threadpool(_service(request).ingest_document, owner, body)


@router.get("/documents", response_model=KnowledgeDocumentPage, responses=_ERRORS)
async def list_documents(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> KnowledgeDocumentPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.KNOWLEDGE_READ)
    return await run_in_threadpool(
        _service(request).list_documents, owner, limit=limit, offset=offset
    )


@router.get(
    "/documents/{document_id}",
    response_model=KnowledgeDocumentDetail,
    responses=_ERRORS,
)
async def get_document(
    request: Request,
    document_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> KnowledgeDocumentDetail:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.KNOWLEDGE_READ)
    doc = await run_in_threadpool(_service(request).get_document, owner, document_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested knowledge document was not found.",
        )
    return doc


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_ERRORS,
)
async def delete_document(
    request: Request,
    document_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> Response:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.KNOWLEDGE_WRITE)
    deleted = await run_in_threadpool(
        _service(request).delete_document, owner, document_id
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested knowledge document was not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================= Search & Budget

@router.post("/search", response_model=KnowledgeSearchResponse, responses=_ERRORS)
async def search_knowledge(
    request: Request,
    body: KnowledgeSearchRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> KnowledgeSearchResponse:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.KNOWLEDGE_READ)
    return await run_in_threadpool(_service(request).search, owner, body)


@router.post(
    "/context-budget", response_model=ContextBudgetAllocation, responses=_ERRORS
)
async def compute_context_budget(
    request: Request,
    body: ContextBudgetRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ContextBudgetAllocation:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.KNOWLEDGE_READ)
    return await run_in_threadpool(
        _service(request).allocate_context_budget, owner, body
    )
