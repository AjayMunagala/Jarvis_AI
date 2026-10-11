# JARVIS Roadmap

Roadmap items are goals, not claims about current functionality.

## Core v0.1 (complete)

- Modular FastAPI monolith with validated settings, health/readiness, mock chat, bounded provider execution, and tests.

## Core v0.2 — Mother Orchestrator (implemented)

- Add explicit conversation/request context and a small orchestration service between HTTP and providers.
- Add provider contract conformance tests and a configurable fake provider for timeout/failure cases.
- Define capability registry and tool interface types without executing external actions.
- Define audit event schemas and document production boundaries; keep the demo runnable offline.

Core v0.2 currently uses deterministic intent rules and the mock provider. It does not implement specialist model reasoning, persistent audit storage, or tool execution. Tool configuration is empty by design.

## Core v0.3 — Real LLM Provider Foundation (implemented; live access unverified)

- Add one optional OpenAI Chat Completions adapter behind the existing typed provider protocol.
- Keep deterministic mock mode as the default; configure OpenAI credentials, model, endpoint, and connection/read timeouts through environment variables.
- Validate provider output, sanitize errors, and retry only eligible transient failures within the existing orchestration request deadline.
- Give the validation expert a distinct configured instruction for model providers, without claiming that the mock or all seven personas perform distinct model reasoning.
- Preserve the chat response fields and mark model responses with provider identity and `demo=false`.

The adapter is verified with mocked HTTP transport only. No live credentials or model endpoint were available for verification. It does not add tools, external actions, or persistence.

## Core v0.4 — Specialist Intelligence and Provider Reliability (implemented)

- Give the AnalysisExpert and ValidationExpert distinct configured instructions through the existing provider interface; retain deterministic mock behavior, which ignores those instructions.
- Validate provider responses as nonblank bounded text and reject refusal, malformed, empty, and whitespace-only OpenAI completions as sanitized permanent failures.
- Keep bounded execution deadlines, transient-only retries, partial results, trace metadata, API response metadata, and the advisory-only boundary.

Offline regression tests for Core v0.4 use controlled mock providers and HTTP transport to assert specialist prompt selection and transient failure handling. No live provider call or semantic model-quality evaluation was performed. In the restricted sandbox, asyncio loop startup is blocked because local socketpair creation attempts a restricted localhost accept.

## Core v0.5 — Structured Specialist Outputs (implemented)

- Add typed, bounded Pydantic contracts for AnalysisExpert and ValidationExpert, including evidence rules for supported conclusions and validation findings.
- Extend the vendor-neutral provider contract with an optional response-model request; transform a copied strict Chat Completions JSON Schema by removing size bounds and documented unsupported composition keywords, verify required/closed objects and references, and validate returned JSON locally with all original Pydantic bounds before synthesis.
- Keep deterministic mock behavior and the existing `/chat` response shape. Mock specialist outputs use `not_evaluated` and preserve the established demo response text.
- Render validated specialist fields into readable response text, use a bounded 8,192 completion-token allowance for structured requests (1,024 for ordinary text), and treat unsupported schemas, malformed JSON, refusals, and truncated structured completions as sanitized permanent failures.

The new behavior is covered by offline mocked-provider tests, including actual outgoing schemas for both specialists. OpenAI's guide lists array bounds as unsupported on fine-tuned models; because provider model selection is configurable, the adapter omits all schema size-bound keywords and retains those checks in local Pydantic validation. No live API request, strict-schema endpoint compatibility, or semantic quality evaluation has been performed. Structured contracts constrain shape and local bounds; they do not prove factual correctness. No tools, external actions, persistence, or new specialist agents are introduced.

## Core v0.6 — Optional Conversation Persistence Foundation (implemented; live PostgreSQL unverified)

- Add a database-independent conversation repository contract and optional PostgreSQL adapter using parameterized psycopg operations.
- Keep persistence disabled by default and leave PostgreSQL outside normal startup and mock-mode test requirements.
- Store each successful chat request as a new conversation with exactly its submitted user message and final assistant reply in one transaction. A save failure returns sanitized HTTP 503 rather than an ordinary success response.
- Add migration `0002_conversation_persistence.sql` with UUID identifiers, a conversation foreign key, restricted roles, nonblank bounded content, timestamps, and an ordered-message index.
- Keep the public `/chat` response shape, provider boundary, and advisory-only behavior unchanged. No history API, multi-turn linking, frontend history, accounts, audit traces, or provider internals are added.

Repository transaction behavior and API failure/compatibility paths are tested with controlled doubles. No `psql` executable or installed psycopg extra was available, so migration application and live PostgreSQL behavior remain unverified.

## Later milestones

1. Conversation continuity and memory: design client conversation identity, retrieval APIs, retention, and user isolation before adding continuity or memory.
2. Real model adapters: optional local CPU model and compatible hosted providers behind provider interfaces, with explicit cost, privacy, timeout, and retry controls.
3. Safe tools and permissions: tool registry/runtime, permission and approval gateway, independent verification, bounded retries, and fault isolation.
4. User experience: React/TypeScript interface, natural conversation, voice input/output, and supported desktop/mobile integrations.
5. Personal knowledge: document analysis, web research, memory, retrieval, and personalized learning/tutoring.
6. Personal workflows: task management, reminders, scheduling, and proactive monitoring/notifications.
7. Specialized capabilities: travel planning, creative tools, and smart-home integrations behind explicit user permissions.
8. High-risk domains: financial planning, expense analysis, stock-market research, and experimental forecasting with clear limitations and no transactions without explicit authorization.
9. Defensive operations: system health monitoring and defensive cybersecurity, with careful authorization and auditability.
10. Controlled improvement: model evaluation, fine-tuning experiments, versioning, and rollback with human review.

The long-term vision includes natural conversation and voice; desktop and supported mobile-device integrations; coding, debugging, test execution, fixes and independent verification; document analysis, web research, personal knowledge and long-term memory; task management, reminders, scheduling and personal workflows; financial planning, expense analysis, stock-market research and experimental forecasting; proactive monitoring and notifications; smart-home integrations; defensive cybersecurity and system health monitoring; personalized learning and tutoring; travel planning and creative tools; and controlled self-improvement, model evaluation, fine-tuning experiments and rollback.
