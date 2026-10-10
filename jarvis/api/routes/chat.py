"""Initial chat endpoint with bounded provider execution."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from jarvis.models.schemas import ChatRequest, ChatResponse, ErrorResponse

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
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    if len(body.message) > request.app.state.settings.max_message_length:
        raise HTTPException(status_code=413, detail="Message exceeds the configured size limit.")
    try:
        reply = await asyncio.wait_for(
            request.app.state.provider.generate_reply(body.message),
            timeout=request.app.state.settings.request_timeout_seconds,
        )
    except TimeoutError as exc:
        logger.warning("Chat provider timed out")
        raise HTTPException(status_code=504, detail="The configured provider timed out.") from exc
    except Exception as exc:
        logger.error("Chat provider failed", extra={"error_type": type(exc).__name__})
        raise HTTPException(status_code=502, detail="The configured provider failed.") from exc
    return ChatResponse(reply=reply, provider=request.app.state.provider.name, demo=request.app.state.provider.is_demo)
