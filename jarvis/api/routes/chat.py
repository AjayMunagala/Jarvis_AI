"""Chat endpoint that delegates bounded work to the Mother Orchestrator."""

import asyncio
import logging
from time import monotonic
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from jarvis.api.dependencies import authenticated_owner, enforce_capability
from jarvis.models.schemas import (
    ChatContextSource,
    ChatContextSummary,
    ChatMemoryContext,
    ChatRequest,
    ChatResponse,
    ErrorResponse,
)
from jarvis.agent.loop import ActionLoop
from jarvis.agent.verification import verify
from jarvis.memory.base import MemoryDeadlineExceeded
from jarvis.memory.context import AssembledContext, HistoryEntry, assemble_context, cited_refs
from jarvis.memory.query import extract_query_terms
from jarvis.memory.retrieval import retrieve_context
from jarvis.orchestration.models import (
    ExpertExecutionError,
    ExpertTimeoutError,
    RequestContext,
    RequestDeadlineExceeded,
)
from jarvis.persistence.base import ConversationNotFound, PersistenceError
from jarvis.security.policy import Capability
from jarvis.tools.handlers import build_tool_context

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
    settings = request.app.state.settings
    enforce_capability(
        request, owner_id, Capability.CHAT_ADVISORY, allow_stateless_chat=True
    )
    if len(body.message) > settings.max_message_length:
        raise HTTPException(status_code=413, detail="Message exceeds the configured size limit.")
    context = RequestContext(message=body.message)
    deadline = monotonic() + settings.request_timeout_seconds
    response.headers["X-JARVIS-Execution-ID"] = context.execution_id
    repository = request.app.state.conversation_repository
    load_history = (
        repository is not None
        and body.conversation_id is not None
        and settings.context_history_messages > 0
    )
    if repository is not None:
        enforce_capability(request, owner_id, Capability.CONVERSATION_WRITE)
        if settings.memory_retrieval_enabled:
            enforce_capability(request, owner_id, Capability.MEMORY_READ)
        if load_history:
            enforce_capability(request, owner_id, Capability.CONVERSATION_READ)
    if body.conversation_id is not None and repository is None:
        raise HTTPException(
            status_code=503,
            detail="Conversation persistence is not enabled.",
            headers={"X-JARVIS-Execution-ID": context.execution_id},
        )

    history: tuple[HistoryEntry, ...] = ()
    if repository is None or body.conversation_id is None:
        history_status = "not_requested"
    elif not load_history:
        history_status = "disabled"
    else:
        # Loading history first also rejects an unknown conversation before any
        # provider call is made.
        history, history_status = await _load_history(
            repository, body.conversation_id,
            limit=settings.context_history_messages,
            deadline=deadline, execution_id=context.execution_id,
        )

    retrieval = None
    if settings.memory_retrieval_enabled:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise _request_timeout(context.execution_id)
        retrieval_task = asyncio.create_task(run_in_threadpool(
            retrieve_context,
            request.app.state.memory_repository,
            body.message,
            owner_id=owner_id,
            limit=settings.memory_retrieval_limit,
            execution_id=context.execution_id,
            deadline=deadline,
            document_limit=settings.document_retrieval_limit,
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

    assembled: AssembledContext | None = None
    if repository is not None:
        assembled = assemble_context(
            history=history,
            retrieved=retrieval.records if retrieval is not None else (),
            terms=extract_query_terms(body.message),
            history_status=history_status,
            retrieval_status=retrieval.outcome if retrieval is not None else "disabled",
            history_limit=settings.context_history_messages,
            excerpt_max_chars=settings.context_excerpt_max_chars,
            max_chars=settings.context_max_chars,
        )
        context = context.model_copy(update={
            "context": assembled,
            "retrieved_memories": retrieval.records if retrieval is not None else (),
        })

    tool_activity = None
    tool_evidence = ()
    runtime = getattr(request.app.state, "tool_runtime", None)
    if settings.chat_tools_enabled and owner_id is not None and runtime is not None:
        # Proposals (rule-based or model) are validated and executed only through the
        # read-only ToolRuntime, inside this request's shared deadline.
        tool_activity, tool_evidence = await ActionLoop(runtime, request.app.state.provider, settings).run(
            owner_id=owner_id, message=body.message, context=build_tool_context(request.app.state, owner_id),
            policy=request.app.state.authorization_policy, deadline=deadline, execution_id=context.execution_id,
        )
        context = context.model_copy(update={"tool_evidence": tool_evidence})
        if monotonic() >= deadline:
            raise _request_timeout(context.execution_id)

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

    memory_context = None
    if retrieval is not None:
        included = {item.source_id for item in assembled.items} if assembled is not None else set()
        memory_context = ChatMemoryContext(
            outcome=retrieval.outcome,
            memory_ids=[record.id for record in retrieval.records if record.id in included],
        )
    return ChatResponse(
        reply=result.reply,
        provider=request.app.state.provider.name,
        demo=request.app.state.provider.is_demo,
        partial=result.partial,
        conversation_id=conversation_id,
        memory_context=memory_context,
        context=_context_summary(assembled, result.reply) if assembled is not None else None,
        tool_activity=(tool_activity.model_copy(update={"verification": verify(result.reply, tool_activity, tool_evidence)})
                       if tool_activity is not None else None),
    )


async def _load_history(
    repository, conversation_id: UUID, *, limit: int, deadline: float, execution_id: str
) -> tuple[tuple[HistoryEntry, ...], str]:
    """Load recent turns within the request deadline; degrade on storage failure."""
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _request_timeout(execution_id)
    task = asyncio.create_task(run_in_threadpool(
        _fetch_recent_messages, repository, conversation_id, limit, deadline
    ))
    completed, _ = await asyncio.wait({task}, timeout=remaining)
    if not completed:
        task.add_done_callback(_consume_background_result)
        raise _request_timeout(execution_id)
    try:
        messages = task.result()
    except ConversationNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="The requested conversation was not found.",
            headers={"X-JARVIS-Execution-ID": execution_id},
        ) from exc
    except Exception:
        if monotonic() >= deadline:
            raise _request_timeout(execution_id) from None
        # History is helpful, not required: answer without it and say so.
        logger.warning("Conversation history unavailable for context", extra={"execution_id": execution_id})
        return (), "unavailable"
    entries = []
    for message in tuple(messages)[-limit:]:
        try:
            entries.append(HistoryEntry(
                id=message.id, role=message.role, content=message.content,
                created_at=getattr(message, "created_at", None),
            ))
        except (ValidationError, AttributeError):
            # A malformed stored row is skipped rather than sent to the model.
            continue
    return tuple(entries), ("included" if entries else "empty")


def _fetch_recent_messages(repository, conversation_id: UUID, limit: int, deadline: float):
    recent = getattr(repository, "get_recent_messages", None)
    if callable(recent):
        return recent(conversation_id, limit=limit, deadline=deadline)
    return repository.get_messages(conversation_id, limit=limit)


def _context_summary(assembled: AssembledContext, reply: str) -> ChatContextSummary:
    cited, unknown = cited_refs(reply, assembled)
    return ChatContextSummary(
        history_status=assembled.history_status,
        retrieval_status=assembled.retrieval_status,
        sources=[
            ChatContextSource(
                ref=item.ref,
                source_type=item.source_type,
                source_id=item.source_id,
                label=item.label,
                role=item.role,
                timestamp=item.timestamp,
                excerpt_chars=len(item.excerpt),
                truncated=item.truncated,
                cited_in_reply=item.ref in cited,
            )
            for item in assembled.items
        ],
        omitted_count=assembled.omitted_count,
        total_chars=assembled.total_chars,
        unknown_citation_count=unknown,
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
