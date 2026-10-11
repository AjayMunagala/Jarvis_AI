# JARVIS Core Architecture

## Current shape

This is a modular monolith. A single FastAPI process owns HTTP routing, configuration, provider selection, and the chat flow. `/chat` delegates to a Mother Orchestration service that classifies intent, plans bounded expert steps, executes configured experts and synthesizes validated outputs. Modules communicate through typed schemas and the `IntelligenceProvider` protocol.

- `jarvis/main.py`: application factory, validated startup settings, dependency construction, lifespan, and sanitized exception handling.
- `jarvis/core/config.py`: Pydantic settings, environment loading, and validation.
- `jarvis/core/logging_config.py`: structured JSON logs on the application logger. It preserves root/host handlers and excludes chat contents and exception messages/tracebacks; exception type is retained for diagnosis without logging potentially sensitive details.
- `jarvis/core/middleware.py`: caps HTTP request body bytes while receiving the request and returns a sanitized 413 response when exceeded.
- `jarvis/api/routes/`: health/readiness and chat HTTP endpoints.
- `jarvis/models/schemas.py`: public request and response contracts.
- `jarvis/intelligence/base.py`: provider contract; `mock_provider.py` is an offline demonstration; `router.py` selects an adapter.
- `jarvis/orchestration/`: request context, intent and plan contracts, deterministic classifier, bounded planner, configured expert/tool registries, executor, synthesis, and in-memory audit-event schemas.
- `jarvis/orchestration/config/experts.json`: enabled expert personas and intent routing. `tools.json` defines the tool registry contract and currently contains no tools.
- `tests/`: API behavior and configuration tests.

## Request flow

`POST /chat` is subject to the global request body byte ceiling, validates the message, enforces the configured character ceiling, and passes an explicit request context to the orchestrator. Deterministic keyword classification falls back to `general_inquiry` at confidence 0 if classification raises or returns an invalid result. The planner selects only enabled experts from the validated JSON registry and caps plans at five rounds. Sequential execution is the default; parallel execution requires the matching intent to be listed in `parallel_intents`, uses a semaphore, and is capped by validated settings. Provider output is validated with Pydantic before synthesis. Only explicit transient expert failures are retried with bounded exponential backoff; attempts share the request deadline. Failed steps produce partial output when successful expert results remain. API errors use a common `error` and `detail` JSON envelope; `/chat` keeps the legacy fields and adds a `partial` boolean. Successful responses also carry an `X-JARVIS-Execution-ID` correlation header. Oversized streamed request bodies are stopped and returned as 413; HTTP/1.x rejection responses request connection close so unread bytes cannot be reused as another request. Logs and in-memory audit events carry execution/expert metadata but exclude messages, output content, secrets, and exception details.

Expert definitions are persona metadata, not independent agent implementations: every configured expert currently calls the same provider contract, and the mock provider returns deterministic demo text. Tool definitions are configuration data only. There is no tool executor, permission gateway, persistence, or external action path in this milestone. Audit events are returned in the internal orchestration result and are not persisted or exposed in the public chat body.

## Adding capabilities and providers

Future model-driven planners/classifiers, memory, capability registry, tool runtime, permission gateway, retrieval, and independent verification modules should expose small typed interfaces and be assembled in the application/orchestration layer. Capability code should depend on those interfaces rather than a vendor SDK. A new model adapter implements `IntelligenceProvider`; configuration and router registration then select it. Keep credentials in environment-backed secret settings, never in logs or source. Tools that can affect external systems should be mediated by a separate permission/approval boundary and explicit bounded execution policy.

## Reliability and future persistence

Current request processing has a configurable timeout and sanitized failure boundary. The repository now includes standalone PostgreSQL provisioning and a versioned migration ledger under `database/`, but the FastAPI application does not connect to it. The only database object currently justified is the migration metadata table; conversation persistence, audit-event persistence, application roles, rate limiting, authentication, and multi-user isolation remain deferred until their requirements are designed. The health endpoint is process-level; readiness currently means startup configuration/provider construction succeeded.
