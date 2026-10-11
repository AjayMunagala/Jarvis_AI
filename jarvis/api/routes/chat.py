"""Chat endpoint that delegates bounded work to the Mother Orchestrator."""

import logging

from fastapi import APIRouter, HTTPException, Request, Response

from jarvis.models.schemas import ChatRequest, ChatResponse, ErrorResponse
from jarvis.orchestration.models import ExpertExecutionError, ExpertTimeoutError, RequestContext

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


@router.post(
    "/chat",
    response_model=ChatResponse,
    responses={
        413: {"model": ErrorResponse, "description": "Request too large"},
        422: {"model": ErrorResponse, "description": "Invalid request"},
        502: {"model": ErrorResponse, "description": "Provider failure"},
        504: {"model": ErrorResponse, "description": "Provider timeout"},
    },
)
async def chat(body: ChatRequest, request: Request, response: Response) -> ChatResponse:
    if len(body.message) > request.app.state.settings.max_message_length:
        raise HTTPException(status_code=413, detail="Message exceeds the configured size limit.")
    context = RequestContext(message=body.message)
    response.headers["X-JARVIS-Execution-ID"] = context.execution_id
    try:
        result = await request.app.state.orchestration_service.run(context)
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
    return ChatResponse(
        reply=result.reply,
        provider=request.app.state.provider.name,
        demo=request.app.state.provider.is_demo,
        partial=result.partial,
    )
