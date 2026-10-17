"""Read-only conversation history endpoint."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.models.schemas import (
    ConversationHistoryResponse,
    ConversationListResponse,
    ErrorResponse,
    HistoryMessage,
)
from jarvis.persistence.base import ConversationNotFound, PersistenceError
from jarvis.security.policy import Capability

router = APIRouter(tags=["conversations"])


@router.get(
    "/conversations",
    response_model=ConversationListResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Authentication required"},
        403: {"model": ErrorResponse, "description": "Operation not permitted"},
        422: {"model": ErrorResponse, "description": "Invalid pagination parameters"},
        503: {"model": ErrorResponse, "description": "Persistence unavailable"},
    },
)
async def list_conversations(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ConversationListResponse:
    repository = request.app.state.conversation_repository
    if repository is None:
        raise HTTPException(
            status_code=503,
            detail="Conversation persistence is not enabled.",
        )
    enforce_capability(request, owner_id, Capability.CONVERSATION_READ)
    try:
        return await run_in_threadpool(
            repository.list_conversations,
            limit=limit,
            offset=offset,
        )
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail="Conversation list is unavailable.") from exc


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=ConversationHistoryResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Conversation not found"},
        503: {"model": ErrorResponse, "description": "Persistence unavailable"},
    },
)
async def conversation_messages(
    conversation_id: UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    before_sequence: int | None = Query(default=None, ge=1),
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ConversationHistoryResponse:
    repository = request.app.state.conversation_repository
    if repository is None:
        raise HTTPException(
            status_code=503,
            detail="Conversation persistence is not enabled.",
        )
    enforce_capability(request, owner_id, Capability.CONVERSATION_READ)
    try:
        messages = await run_in_threadpool(
            repository.get_messages,
            conversation_id,
            before_sequence=before_sequence,
            limit=limit + 1,
        )
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail="The requested conversation was not found.") from exc
    except PersistenceError as exc:
        raise HTTPException(status_code=503, detail="Conversation history is unavailable.") from exc

    has_more = len(messages) > limit
    page = messages[-limit:] if has_more else messages
    next_before = page[0].sequence_number if has_more and page else None
    return ConversationHistoryResponse(
        conversation_id=conversation_id,
        messages=[
            HistoryMessage(
                id=item.id,
                conversation_id=item.conversation_id,
                sequence_number=item.sequence_number,
                role=item.role,
                content=item.content,
                created_at=item.created_at,
            )
            for item in page
        ],
        has_more=has_more,
        next_before_sequence=next_before,
    )
