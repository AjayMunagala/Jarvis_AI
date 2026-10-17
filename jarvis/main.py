"""FastAPI application factory and lifecycle."""

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from jarvis import __version__
from jarvis.api.routes.activity import router as activity_router
from jarvis.api.routes.browser import router as browser_router
from jarvis.api.routes.chat import router as chat_router
from jarvis.api.routes.coding import router as coding_router
from jarvis.api.routes.conversations import router as conversations_router
from jarvis.api.routes.health import router as health_router
from jarvis.advanced_intelligence import build_intelligence
from jarvis.api.routes.intelligence import router as intelligence_router
from jarvis.api.routes.interface import router as interface_router
from jarvis.api.routes.memories import router as memories_router
from jarvis.api.routes.repo import router as repo_router
from jarvis.api.routes.tools import router as tools_router
from jarvis.api.routes.verification import router as verification_router
from jarvis.audit.service import ApprovalService, AuditService
from jarvis.audit.stores import (
    MemoryApprovalStore, MemoryAuditStore, PostgresApprovalStore, PostgresAuditStore,
)
from jarvis.browser.service import BrowserService
from jarvis.core.config import Settings, get_settings
from jarvis.core.logging_config import configure_logging
from jarvis.core.middleware import RequestBodyLimitMiddleware
from jarvis.intelligence.base import IntelligenceProvider
from jarvis.intelligence.router import build_provider
from jarvis.orchestration.service import OrchestrationService
from jarvis.persistence.postgres import PostgresConversationRepository
from jarvis.persistence.memory_postgres import PostgresMemoryRepository
from jarvis.repo.service import RepoIntelligenceService
from jarvis.security.policy import CapabilityPolicy
from jarvis.tools.runtime import ToolRuntime
from jarvis.verification.service import VerificationService

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    provider: IntelligenceProvider | None = None,
) -> FastAPI:
    # Revalidate explicitly supplied Settings too; model_construct/model_copy can
    # bypass Pydantic validators and must not create a persistence auth bypass.
    resolved_settings = (
        Settings.model_validate(settings.__dict__) if settings is not None else get_settings()
    )
    configure_logging(resolved_settings.log_level)
    resolved_provider = provider if provider is not None else build_provider(
        resolved_settings.provider,
        settings=resolved_settings,
    )
    orchestration_service = OrchestrationService(resolved_provider, resolved_settings)
    conversation_repository = None
    memory_repository = None
    if resolved_settings.persistence_enabled:
        database_url = resolved_settings.persistence_database_url
        # Settings validation guarantees the URL exists when this option is enabled.
        assert database_url is not None
        conversation_repository = PostgresConversationRepository(
            database_url.get_secret_value(),
            connect_timeout_seconds=resolved_settings.persistence_connect_timeout_seconds,
            operation_timeout_seconds=resolved_settings.persistence_operation_timeout_seconds,
        )
        memory_repository = PostgresMemoryRepository(
            database_url.get_secret_value(),
            connect_timeout_seconds=resolved_settings.persistence_connect_timeout_seconds,
            operation_timeout_seconds=resolved_settings.persistence_operation_timeout_seconds,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("JARVIS starting", extra={"environment": resolved_settings.environment})
        yield
        # Browser contexts hold page state; close every session on shutdown.
        app.state.browser_service.close_all()
        app.state.tool_runtime.close()
        logger.info("JARVIS shutting down")

    app = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.authorization_policy = CapabilityPolicy(resolved_settings.owner_id)
    app.state.provider = resolved_provider
    app.state.orchestration_service = orchestration_service
    app.state.conversation_repository = conversation_repository
    app.state.memory_repository = memory_repository
    repo_service = RepoIntelligenceService()
    app.state.repo_service = repo_service
    app.state.verification_service = VerificationService(repo_service.repo_root, resolved_settings)
    # Constructed even when disabled so /browser/status can report configuration;
    # providers import optional engines lazily and open nothing at startup.
    app.state.browser_service = BrowserService(resolved_settings)
    # The runtime binds registry entries to allowlisted handlers; it is constructed
    # even when disabled so /capabilities can describe the registry.
    app.state.tool_runtime = ToolRuntime(
        resolved_settings, orchestration_service.tool_registry, app.state.authorization_policy,
    )
    # v2.9 audit trail and approval gateway: durable in PostgreSQL when persistence is
    # enabled (decision D7), otherwise in memory and labelled non-durable.
    if resolved_settings.persistence_enabled:
        url = resolved_settings.persistence_database_url.get_secret_value()
        timeouts = dict(connect_timeout_seconds=resolved_settings.persistence_connect_timeout_seconds,
                        operation_timeout_seconds=resolved_settings.persistence_operation_timeout_seconds)
        audit_store, approval_store = PostgresAuditStore(url, **timeouts), PostgresApprovalStore(url, **timeouts)
    else:
        audit_store, approval_store = MemoryAuditStore(), MemoryApprovalStore()
    app.state.audit_service = AuditService(audit_store)
    app.state.approval_service = ApprovalService(
        approval_store, app.state.audit_service, ttl_seconds=resolved_settings.approvals_ttl_seconds,
    )
    # v5.1-v5.6 advanced intelligence layer (wraps the existing provider; replaces nothing).
    app.state.intelligence = build_intelligence(resolved_settings, resolved_provider, app.state.audit_service)
    for component in (app.state.tool_runtime, app.state.browser_service):
        component.audit_service = app.state.audit_service
        component.approval_service = app.state.approval_service
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=resolved_settings.max_request_body_bytes,
    )
    app.include_router(health_router)
    app.include_router(chat_router)
    app.include_router(conversations_router)
    app.include_router(memories_router)
    app.include_router(repo_router)
    app.include_router(coding_router)
    app.include_router(verification_router)
    app.include_router(browser_router)
    app.include_router(tools_router)
    app.include_router(activity_router)
    app.include_router(interface_router)
    app.include_router(intelligence_router)

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
            401: "unauthorized",
            502: "provider_error",
            504: "provider_timeout",
            503: "persistence_unavailable",
        }
        error_code = (
            "request_timeout"
            if exc.status_code == 504 and exc.detail == "The overall chat request timed out."
            else "conversation_not_found"
            if exc.status_code == 404 and exc.detail == "The requested conversation was not found."
            else "memory_not_found"
            if exc.status_code == 404 and exc.detail == "The requested memory was not found."
            else error_codes.get(exc.status_code, "http_error")
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": error_code,
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
