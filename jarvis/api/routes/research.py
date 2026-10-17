"""Authenticated research and document intelligence API routes (v3.4).

Provides multi-format document parsing, injection fencing, and cited synthesis.
Owner-isolated and capability-checked.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.models.schemas import ErrorResponse
from jarvis.research.models import (
    DocumentParseRequest,
    ParsedDocument,
    ParsedDocumentPage,
    ResearchSynthesis,
    ResearchSynthesisPage,
    ResearchSynthesizeRequest,
)
from jarvis.research.parsers import DocumentParsingError, DocumentTooLargeError
from jarvis.research.service import ResearchService
from jarvis.security.policy import Capability

router = APIRouter(prefix="/research", tags=["research"])
_ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 413, 422, 503)}


def _owner(owner_id: UUID | None) -> UUID:
    if owner_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid bearer credential is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return owner_id


def _service(request: Request) -> ResearchService:
    service = getattr(request.app.state, "research_service", None)
    if not isinstance(service, ResearchService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The research service is unavailable.",
        )
    return service


# ============================================================= Documents

@router.post(
    "/documents/parse",
    response_model=ParsedDocument,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def parse_document(
    request: Request,
    body: DocumentParseRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ParsedDocument:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.RESEARCH_WRITE)
    try:
        return await run_in_threadpool(
            _service(request).parse_and_store_document, owner, body
        )
    except DocumentTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except DocumentParsingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.get("/documents", response_model=ParsedDocumentPage, responses=_ERRORS)
async def list_documents(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ParsedDocumentPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.RESEARCH_READ)
    return await run_in_threadpool(
        _service(request).list_documents, owner, limit=limit, offset=offset
    )


@router.get(
    "/documents/{document_id}",
    response_model=ParsedDocument,
    responses=_ERRORS,
)
async def get_document(
    request: Request,
    document_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ParsedDocument:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.RESEARCH_READ)
    doc = await run_in_threadpool(_service(request).get_document, owner, document_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested parsed document was not found.",
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
    enforce_capability(request, owner, Capability.RESEARCH_WRITE)
    deleted = await run_in_threadpool(
        _service(request).delete_document, owner, document_id
    )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested parsed document was not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ============================================================= Synthesis

@router.post(
    "/synthesize",
    response_model=ResearchSynthesis,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def synthesize_research(
    request: Request,
    body: ResearchSynthesizeRequest,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ResearchSynthesis:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.RESEARCH_WRITE)
    return await run_in_threadpool(
        _service(request).synthesize_research, owner, body
    )


@router.get("/syntheses", response_model=ResearchSynthesisPage, responses=_ERRORS)
async def list_syntheses(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ResearchSynthesisPage:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.RESEARCH_READ)
    return await run_in_threadpool(
        _service(request).list_syntheses, owner, limit=limit, offset=offset
    )


@router.get(
    "/syntheses/{synthesis_id}",
    response_model=ResearchSynthesis,
    responses=_ERRORS,
)
async def get_synthesis(
    request: Request,
    synthesis_id: UUID,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ResearchSynthesis:
    owner = _owner(owner_id)
    enforce_capability(request, owner, Capability.RESEARCH_READ)
    synth = await run_in_threadpool(
        _service(request).get_synthesis, owner, synthesis_id
    )
    if synth is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested research synthesis was not found.",
        )
    return synth
