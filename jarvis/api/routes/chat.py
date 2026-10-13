"""Chat endpoint that delegates bounded work to the Mother Orchestrator."""

import asyncio
import logging
from time import monotonic
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner
from jarvis.models.schemas import ChatMemoryContext, ChatRequest, ChatResponse, ErrorResponse
from jarvis.memory.base import MemoryDeadlineExceeded
from jarvis.memory.retrieval import retrieve_context
from jarvis.orchestration.models import (
    ExpertExecutionError,
    ExpertTimeoutError,
    RequestContext,
    RequestDeadlineExceeded,
)
from jarvis.persistence.base import ConversationNotFound, PersistenceError

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


@router.post(
    "/chat",
    response_model=ChatResponse,
    responses={
        413: {"model": ErrorResponse, "description": "Request too large"},
        422: {"model": ErrorResponse, "description": "Invalid request"},
        502: {"model": ErrorResponse, "description": "Provider failure"},
        504: {"model": ErrorResponse, "description": "Request or provider timeout"},
        503: {"model": ErrorResponse, "description": "Persistence unavailable"},
        404: {"model": ErrorResponse, "description": "Conversation not found"},
    },
)
async def chat(
    body: ChatRequest,
    request: Request,
    response: Response,
    owner_id: UUID | None = Depends(authenticated_owner),
) -> ChatResponse:
    if len(body.message) > request.app.state.settings.max_message_length:
        raise HTTPException(status_code=413, detail="Message exceeds the configured size limit.")
    context = RequestContext(message=body.message)
    deadline = monotonic() + request.app.state.settings.request_timeout_seconds
    response.headers["X-JARVIS-Execution-ID"] = context.execution_id
    repository = request.app.state.conversation_repository
    if body.conversation_id is not None and repository is None:
        raise HTTPException(
            status_code=503,
            detail="Conversation persistence is not enabled.",
            headers={"X-JARVIS-Execution-ID": context.execution_id},
        )
    if request.app.state.settings.memory_retrieval_enabled:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise _request_timeout(context.execution_id)
        retrieval_task = asyncio.create_task(run_in_threadpool(
            retrieve_context,
            request.app.state.memory_repository,
            body.message,
            owner_id=owner_id,
            limit=request.app.state.settings.memory_retrieval_limit,
            execution_id=context.execution_id,
            deadline=deadline,
        ))
        completed, _ = await asyncio.wait({retrieval_task}, timeout=remaining)
        if not completed:
            # Do not cancel the threadpool future: cancellation cannot stop a
            # synchronous DB call. PostgreSQL enforces operation-level limits.
            retrieval_task.add_done_callback(_consume_background_result)
            raise _request_timeout(context.execution_id)
        try:
            retrieval = retrieval_task.result()
        except MemoryDeadlineExceeded as exc:
            raise _request_timeout(context.execution_id) from exc
        if monotonic() >= deadline:
            raise _request_timeout(context.execution_id)
        context = context.model_copy(update={"retrieved_memories": retrieval.records})
        memory_context = ChatMemoryContext(
            outcome=retrieval.outcome,
            memory_ids=[record.id for record in retrieval.records],
        )
    else:
        memory_context = None
    try:
        result = await request.app.state.orchestration_service.run(context, deadline=deadline)
    except RequestDeadlineExceeded as exc:
        logger.warning("Chat request deadline expired before expert execution", extra={"execution_id": context.execution_id})
        raise _request_timeout(context.execution_id) from exc
    except ExpertTimeoutError as exc:
        logger.warning("Chat provider timed out", extra={"execution_id": context.execution_id})
        raise HTTPException(
            status_code=504,
            detail="The configured provider timed out.",
            headers={"X-JARVIS-Execution-ID": context.execution_id},
        ) from exc
    except ExpertExecutionError as exc:
        logger.error(
            "Chat provider failed",
            extra={
                "execution_id": context.execution_id,
                "error_type": getattr(exc, "cause_type", type(exc).__name__),
            },
        )
        raise HTTPException(
            status_code=502,
            detail="The configured provider failed.",
            headers={"X-JARVIS-Execution-ID": context.execution_id},
        ) from exc
    conversation_id = None
    if repository is not None:
        try:
            conversation_id = await run_in_threadpool(
                repository.save_exchange, body.message, result.reply, body.conversation_id
            )
        except ConversationNotFound as exc:
            raise HTTPException(
                status_code=404,
                detail="The requested conversation was not found.",
                headers={"X-JARVIS-Execution-ID": context.execution_id},
            ) from exc
        except PersistenceError as exc:
            logger.error(
                "Chat persistence failed",
                extra={"execution_id": context.execution_id, "error_type": type(exc).__name__},
            )
            raise HTTPException(
                status_code=503,
                detail="The response could not be saved.",
                headers={"X-JARVIS-Execution-ID": context.execution_id},
            ) from exc
    return ChatResponse(
        reply=result.reply,
        provider=request.app.state.provider.name,
        demo=request.app.state.provider.is_demo,
        partial=result.partial,
        conversation_id=conversation_id,
        memory_context=memory_context,
    )


def _request_timeout(execution_id: str) -> HTTPException:
    return HTTPException(
        status_code=504,
        detail="The overall chat request timed out.",
        headers={"X-JARVIS-Execution-ID": execution_id},
    )


def _consume_background_result(task: asyncio.Task) -> None:
    """Consume a late threadpool wrapper result without logging private errors."""
    try:
        task.result()
    except BaseException:
        pass
