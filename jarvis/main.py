"""FastAPI application factory and lifecycle."""

import logging
import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from jarvis import __version__
from jarvis.api.routes.activity import router as activity_router
from jarvis.api.routes.assistant import router as assistant_router
from jarvis.api.routes.browser import router as browser_router
from jarvis.api.routes.chat import router as chat_router
from jarvis.api.routes.coding import router as coding_router
from jarvis.api.routes.conversations import router as conversations_router
from jarvis.api.routes.health import router as health_router
from jarvis.advanced_intelligence import build_intelligence
from jarvis.api.routes.intelligence import router as intelligence_router
from jarvis.api.routes.interface import router as interface_router
from jarvis.api.routes.status import router as status_router
from jarvis.api.routes.usage import router as usage_router
from jarvis.usage.service import MemoryUsageStore, PostgresUsageStore, Price, UsageService
from jarvis.api.routes.memories import router as memories_router
from jarvis.api.routes.repo import router as repo_router
from jarvis.api.routes.tools import router as tools_router
from jarvis.api.routes.verification import router as verification_router
from jarvis.api.routes.workflow import router as workflow_router
from jarvis.api.routes.knowledge import router as knowledge_router
from jarvis.api.routes.research import router as research_router
from jarvis.api.routes.scheduler import router as scheduler_router
from jarvis.api.routes.health_diagnostic import router as health_diagnostic_router
from jarvis.api.routes.finance import router as finance_router
from jarvis.api.routes.improvement import router as improvement_router
from jarvis.api.routes.autonomous import router as autonomous_router
from jarvis.api.routes.integrations import router as integrations_router
from jarvis.api.routes.tutor import router as tutor_router
from jarvis.tutor.service import TutorService
from jarvis.tutor.stores import MemoryTutorStore, PostgresTutorStore
from jarvis.integrations.service import IntegrationService
from jarvis.integrations.stores import MemoryEnrollmentStore, PostgresEnrollmentStore
from jarvis.autonomous.coordinator import AutonomousCoordinator
from jarvis.assistant.service import AssistantService
from jarvis.assistant.stores import MemoryAssistantStore, PostgresAssistantStore
from jarvis.workflow.service import WorkflowService
from jarvis.workflow.stores import MemoryWorkflowStore, PostgresWorkflowStore
from jarvis.knowledge.service import KnowledgeService
from jarvis.knowledge.stores import MemoryKnowledgeStore, PostgresKnowledgeStore
from jarvis.research.service import ResearchService
from jarvis.research.stores import MemoryResearchStore, PostgresResearchStore
from jarvis.assistant.briefing import build_briefing
from jarvis.scheduler.cron import ScheduleError
from jarvis.scheduler.runner import SchedulerRunner
from jarvis.scheduler.service import SchedulerService, parse_quiet_hours
from jarvis.scheduler.stores import MemorySchedulerStore, PostgresSchedulerStore
from jarvis.health.service import HealthService
from jarvis.health.stores import MemoryHealthStore, PostgresHealthStore
from jarvis.finance.service import FinanceService
from jarvis.finance.stores import MemoryFinanceStore, PostgresFinanceStore
from jarvis.improvement.service import ImprovementService
from jarvis.improvement.stores import ImprovementStorageError, MemoryImprovementStore, PostgresImprovementStore
from jarvis.audit.service import ApprovalError, ApprovalRequired, ApprovalService, AuditService
from jarvis.audit.models import ApprovalRequiredResponse
from jarvis.tools.handlers import build_tool_context
from jarvis.audit.stores import (
    MemoryApprovalStore, MemoryAuditStore, PostgresApprovalStore, PostgresAuditStore,
)
from jarvis.browser.service import BrowserService
from jarvis.core.config import Settings, get_settings
from jarvis.core.logging_config import configure_logging
from jarvis.core.correlation import CorrelationIdMiddleware
from jarvis.core.failure_audit import FailedMutationAuditMiddleware
from jarvis.core.middleware import RequestBodyLimitMiddleware
from jarvis.intelligence.base import IntelligenceProvider
from jarvis.intelligence.router import build_provider
from jarvis.orchestration.service import OrchestrationService
from jarvis.persistence.postgres import PostgresConversationRepository
from jarvis.persistence.memory_postgres import PostgresMemoryRepository
from jarvis.repo.service import RepoIntelligenceService
from jarvis.security.policy import CapabilityPolicy
from jarvis.status.service import StatusService
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

    def _recover_scheduler() -> None:
        try:  # v4.5: jobs left "running" by a previous process are marked failed
            app.state.scheduler_service.recover_stale_runs(resolved_settings.owner_id)
        except Exception as exc:  # noqa: BLE001 - startup continues; the jobs stay visible as they are
            logger.warning("Interrupted scheduler jobs could not be recovered", extra={"error_type": type(exc).__name__})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("JARVIS starting", extra={"environment": resolved_settings.environment})
        # v4.4: runs left "running" by a previous process are marked failed, never resumed silently. This runs in
        # the background so an unreachable database cannot delay startup.
        app.state.workflow_service.start_recovery(resolved_settings.owner_id)
        threading.Thread(target=_recover_scheduler, name="jarvis-scheduler-recovery", daemon=True).start()
        if resolved_settings.scheduler_runner_enabled:
            app.state.scheduler_runner.start()
        yield
        app.state.usage_service.close()  # stores queued usage events (bounded wait)
        app.state.scheduler_runner.stop()
        app.state.workflow_service.close()
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
    # v6.1: usage and cost tracking for real providers (durable when persistence is enabled).
    prices = {}
    if resolved_settings.openai_price_input_per_million is not None:  # validated: both prices and a source
        prices[resolved_settings.openai_model] = Price(resolved_settings.openai_price_input_per_million,
                                                       resolved_settings.openai_price_output_per_million,
                                                       resolved_settings.openai_price_source)
    if resolved_settings.openai_fallback_price_input_per_million is not None:
        prices[resolved_settings.openai_fallback_model] = Price(
            resolved_settings.openai_fallback_price_input_per_million,
            resolved_settings.openai_fallback_price_output_per_million, resolved_settings.openai_price_source)
    usage_store = (PostgresUsageStore(
        resolved_settings.persistence_database_url.get_secret_value(),
        connect_timeout_seconds=resolved_settings.persistence_connect_timeout_seconds,
        operation_timeout_seconds=resolved_settings.persistence_operation_timeout_seconds)
        if resolved_settings.persistence_enabled else MemoryUsageStore())
    app.state.usage_service = UsageService(usage_store, prices)
    if hasattr(resolved_provider, "usage_recorder"):
        resolved_provider.usage_recorder = app.state.usage_service.record
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

    # v3.1 personal assistant: tasks, notes, reminders, local calendar.
    if resolved_settings.persistence_enabled:
        url = resolved_settings.persistence_database_url.get_secret_value()
        timeouts = dict(connect_timeout_seconds=resolved_settings.persistence_connect_timeout_seconds,
                        operation_timeout_seconds=resolved_settings.persistence_operation_timeout_seconds)
        assistant_store = PostgresAssistantStore(url, **timeouts)
        workflow_store = PostgresWorkflowStore(url, **timeouts)
        knowledge_store = PostgresKnowledgeStore(url, **timeouts)
        research_store = PostgresResearchStore(url, **timeouts)
        scheduler_store = PostgresSchedulerStore(url, **timeouts)
        health_store = PostgresHealthStore(url, **timeouts)
        finance_store = PostgresFinanceStore(url, **timeouts)
        tutor_store = PostgresTutorStore(url, **timeouts)
    else:
        assistant_store = MemoryAssistantStore()
        workflow_store = MemoryWorkflowStore()
        knowledge_store = MemoryKnowledgeStore()
        research_store = MemoryResearchStore()
        scheduler_store = MemorySchedulerStore()
        health_store = MemoryHealthStore()
        finance_store = MemoryFinanceStore()
        tutor_store = MemoryTutorStore()
    app.state.assistant_service = AssistantService(assistant_store, audit_service=app.state.audit_service)
    app.state.knowledge_service = KnowledgeService(
        knowledge_store,
        audit_service=app.state.audit_service,
    )
    app.state.research_service = ResearchService(
        research_store,
        audit_service=app.state.audit_service,
    )
    # v4.6: parsed research documents can be indexed in the knowledge store and recorded in the knowledge graph.
    app.state.research_service.knowledge_service = app.state.knowledge_service
    app.state.research_service.graph_service = getattr(app.state.intelligence, "graph", None)
    app.state.scheduler_service = SchedulerService(
        scheduler_store,
        audit_service=app.state.audit_service,
    )
    app.state.health_service = HealthService(
        health_store,
        repo_root=repo_service.repo_root,
        audit_service=app.state.audit_service,
    )
    app.state.finance_service = FinanceService(
        finance_store,
        audit_service=app.state.audit_service,
    )
    # v6.5 tutor: goals, plans, exercises and progress; explanations and generation use the resolved provider.
    app.state.tutor_service = TutorService(
        tutor_store, audit_service=app.state.audit_service,
        model_timeout_seconds=resolved_settings.provider_connect_timeout_seconds
        + resolved_settings.provider_read_timeout_seconds * 2 + 5,
    )
    # v4.9: proposals, snapshots and virtual prompt changes are durable when persistence is enabled.
    improvement_store = (PostgresImprovementStore(
        resolved_settings.persistence_database_url.get_secret_value(),
        connect_timeout_seconds=resolved_settings.persistence_connect_timeout_seconds,
        operation_timeout_seconds=resolved_settings.persistence_operation_timeout_seconds)
        if resolved_settings.persistence_enabled else MemoryImprovementStore())
    app.state.improvement_service = ImprovementService(
        improvement_store,
        repo_root=repo_service.repo_root,
        audit_service=app.state.audit_service,
    )
    app.state.workflow_service = WorkflowService(
        workflow_store,
        audit_service=app.state.audit_service,
        assistant_service=app.state.assistant_service,
        knowledge_service=app.state.knowledge_service,
        research_service=app.state.research_service,
        scheduler_service=app.state.scheduler_service,
        health_service=app.state.health_service,
        finance_service=app.state.finance_service,
        tool_runtime=app.state.tool_runtime,
        tool_context_factory=lambda owner: build_tool_context(app.state, owner),
        step_timeout_seconds=resolved_settings.workflow_step_timeout_seconds,
        run_timeout_seconds=resolved_settings.workflow_run_timeout_seconds,
        max_active_runs=resolved_settings.workflow_max_active_runs,
    )
    app.state.autonomous_coordinator = AutonomousCoordinator(
        assistant_service=app.state.assistant_service,
        workflow_service=app.state.workflow_service,
        knowledge_service=app.state.knowledge_service,
        research_service=app.state.research_service,
        scheduler_service=app.state.scheduler_service,
        health_service=app.state.health_service,
        finance_service=app.state.finance_service,
        improvement_service=app.state.improvement_service,
        audit_service=app.state.audit_service,
    )
    # v4.8: integration enrollments (mock adapter only); activation and state-changing actions need approval.
    if resolved_settings.persistence_enabled:
        url = resolved_settings.persistence_database_url.get_secret_value()
        enrollment_store = PostgresEnrollmentStore(
            url, connect_timeout_seconds=resolved_settings.persistence_connect_timeout_seconds,
            operation_timeout_seconds=resolved_settings.persistence_operation_timeout_seconds)
    else:
        enrollment_store = MemoryEnrollmentStore()
    app.state.integration_service = IntegrationService(enrollment_store, audit_service=app.state.audit_service)
    # v4.3: autonomous writes, improvement apply/restore and quarantine/restore wait for an approved request.
    for component in (app.state.autonomous_coordinator, app.state.improvement_service, app.state.health_service,
                      app.state.workflow_service, app.state.integration_service):
        component.approval_service = app.state.approval_service
    # v4.2: one truthful status source, also used by health snapshots, scheduled health checks and the
    # autonomous overview (each previously reported fixed "operational"/"healthy" values).
    app.state.status_service = StatusService(app.state)
    app.state.health_service.status_provider = app.state.status_service.summary_map
    app.state.scheduler_service.health_probe = app.state.status_service.summary_map
    # v4.5: the scheduler can deliver reminders and run workflows (writes still need approval); the background
    # runner only starts when JARVIS_SCHEDULER_RUNNER_ENABLED=true.
    scheduler = app.state.scheduler_service
    scheduler.assistant_service = app.state.assistant_service
    # v6.3: a "daily_briefing" job delivers today's briefing as a notification (owner's local day).
    # Cron expressions are evaluated in UTC: for 07:00 at UTC+05:30 schedule "30 1 * * *".
    scheduler.briefing_builder = lambda owner: build_briefing(
        app.state.assistant_service, owner, utc_offset_minutes=resolved_settings.scheduler_utc_offset_minutes)
    # Scheduled workflows start in the background, so a long run never delays reminders (v4.5 review finding 10).
    scheduler.workflow_trigger = lambda owner, workflow_id: app.state.workflow_service.trigger_run(
        owner, workflow_id, {}, None, False)
    scheduler.quiet_hours = parse_quiet_hours(resolved_settings.scheduler_quiet_hours)
    scheduler.utc_offset_minutes = resolved_settings.scheduler_utc_offset_minutes
    app.state.scheduler_runner = SchedulerRunner(scheduler, resolved_settings.owner_id,
                                                 interval_seconds=resolved_settings.scheduler_tick_seconds)
    app.state.autonomous_coordinator.status_service = app.state.status_service

    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=resolved_settings.max_request_body_bytes,
    )
    # v4.2: added last so they run first (outermost): every request gets a correlation ID, and failed
    # v3.x mutations are audited with it.
    app.add_middleware(FailedMutationAuditMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(health_router)
    app.include_router(health_diagnostic_router)
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
    app.include_router(assistant_router)
    app.include_router(workflow_router)
    app.include_router(knowledge_router)
    app.include_router(research_router)
    app.include_router(scheduler_router)
    app.include_router(finance_router)
    app.include_router(improvement_router)
    app.include_router(autonomous_router)
    app.include_router(intelligence_router)
    app.include_router(status_router)
    app.include_router(usage_router)
    app.include_router(integrations_router)
    app.include_router(tutor_router)

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

    @app.exception_handler(ImprovementStorageError)
    async def improvement_storage_handler(request: Request, exc: ImprovementStorageError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"error": "storage_unavailable",
                                                      "detail": "Improvement storage is unavailable."})

    @app.exception_handler(ScheduleError)
    async def schedule_error_handler(request: Request, exc: ScheduleError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": "invalid_schedule", "detail": str(exc)})

    @app.exception_handler(ApprovalRequired)
    async def approval_required_handler(request: Request, exc: ApprovalRequired) -> JSONResponse:
        # v4.3: the action was not run. 202 because the request was accepted for the owner's decision.
        body = ApprovalRequiredResponse(
            detail=f"Approval required: approve {exc.request.approval_id}, then repeat this request with "
                   f"approval_id={exc.request.approval_id}. Nothing was executed.",
            approval=exc.request)
        return JSONResponse(status_code=202, content=body.model_dump(mode="json"))

    @app.exception_handler(ApprovalError)
    async def approval_error_handler(request: Request, exc: ApprovalError) -> JSONResponse:
        error = {404: "approval_not_found", 409: "approval_not_usable"}.get(exc.status_code, "approval_unavailable")
        return JSONResponse(status_code=exc.status_code, content={"error": error, "detail": exc.public_message})

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Runs in ServerErrorMiddleware, outside CorrelationIdMiddleware, so the ID is read from request state.
        correlation_id = getattr(request.state, "correlation_id", None)
        logger.error("Unhandled application error",
                     extra={"error_type": type(exc).__name__, "correlation_id": correlation_id})
        return JSONResponse(status_code=500, content={"error": "internal_error", "detail": "An unexpected server error occurred."},
                            headers={"x-request-id": correlation_id} if correlation_id else None)

    return app


app = create_app()
