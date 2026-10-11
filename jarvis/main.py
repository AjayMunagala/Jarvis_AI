"""FastAPI application factory and lifecycle."""

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from jarvis import __version__
from jarvis.api.routes.chat import router as chat_router
from jarvis.api.routes.health import router as health_router
from jarvis.core.config import Settings, get_settings
from jarvis.core.logging_config import configure_logging
from jarvis.core.middleware import RequestBodyLimitMiddleware
from jarvis.intelligence.base import IntelligenceProvider
from jarvis.intelligence.router import build_provider
from jarvis.orchestration.service import OrchestrationService

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    provider: IntelligenceProvider | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    resolved_provider = provider if provider is not None else build_provider(resolved_settings.provider)
    orchestration_service = OrchestrationService(resolved_provider, resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("JARVIS starting", extra={"environment": resolved_settings.environment})
        yield
        logger.info("JARVIS shutting down")

    app = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.provider = resolved_provider
    app.state.orchestration_service = orchestration_service
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=resolved_settings.max_request_body_bytes,
    )
    app.include_router(health_router)
    app.include_router(chat_router)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        message_too_long = any(
            error.get("type") == "string_too_long"
            and error.get("loc") == ("body", "message")
            for error in exc.errors()
        )
        if message_too_long:
            return JSONResponse(
                status_code=413,
                content={
                    "error": "request_too_large",
                    "detail": "Message exceeds the maximum supported length.",
                },
            )
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_request", "detail": "The request body is invalid."},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        error_codes = {
            413: "request_too_large",
            502: "provider_error",
            504: "provider_timeout",
        }
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": error_codes.get(exc.status_code, "http_error"),
                "detail": str(exc.detail),
            },
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled application error", extra={"error_type": type(exc).__name__})
        return JSONResponse(status_code=500, content={"error": "internal_error", "detail": "An unexpected server error occurred."})

    return app


app = create_app()
