# JARVIS Core Architecture

## Current shape

This is a modular monolith. A single FastAPI process owns HTTP routing, configuration, provider selection, and the initial chat flow. Modules communicate through typed schemas and the `IntelligenceProvider` protocol.

- `jarvis/main.py`: application factory, validated startup settings, dependency construction, lifespan, and sanitized exception handling.
- `jarvis/core/config.py`: Pydantic settings, environment loading, and validation.
- `jarvis/core/logging_config.py`: structured JSON logs. Chat message contents and configuration secrets are not logged.
- `jarvis/core/middleware.py`: caps HTTP request body bytes while receiving the request and returns a sanitized 413 response when exceeded.
- `jarvis/api/routes/`: health/readiness and chat HTTP endpoints.
- `jarvis/models/schemas.py`: public request and response contracts.
- `jarvis/intelligence/base.py`: provider contract; `mock_provider.py` is an offline demonstration; `router.py` selects an adapter.
- `tests/`: API behavior and configuration tests.

## Request flow

`POST /chat` is subject to the global request body byte ceiling, validates the message, enforces the configured character ceiling, delegates to the provider with a bounded timeout, and returns provider metadata. API errors use a common `error` and `detail` JSON envelope. Provider exceptions are logged by type only and exposed as generic errors. No arbitrary tools or external operations are available.

## Adding capabilities and providers

Future planner, memory, expert/capability registry, tool runtime, permission gateway, retrieval, and independent verification modules should expose small typed interfaces and be assembled in the application/orchestration layer. Capability code should depend on those interfaces rather than a vendor SDK. A new model adapter implements `IntelligenceProvider`; configuration and router registration then select it. Keep credentials in environment-backed secret settings, never in logs or source. Tools that can affect external systems should be mediated by a separate permission/approval boundary and explicit bounded execution policy.

## Reliability and future persistence

Current request processing has a configurable timeout and sanitized failure boundary. PostgreSQL, migrations, audit-event persistence, rate limiting, authentication, and multi-user isolation are not part of v0.1 and must be designed before production use. The health endpoint is process-level; readiness currently means startup configuration/provider construction succeeded.
