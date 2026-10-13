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

## Core v0.7 — Conversation Identity and History API (implemented; live PostgreSQL unverified)

- Extend `/chat` with an optional UUID `conversation_id`; create a new persistent conversation when absent and atomically append exchanges when an existing ID is supplied.
- Return `conversation_id` in the response when persistence is enabled and `null` when disabled, preserving the existing reply/provider/demo/partial fields. Unknown supplied IDs return 404 rather than being replaced.
- Add read-only `GET /conversations/{conversation_id}/messages` with chronological, bounded pages and a sequence cursor. History and continuation return sanitized 503 when persistence is disabled.
- Keep PostgreSQL optional and mock chat functional without it. Conversation IDs do not provide authentication or user ownership; no frontend history UI, account system, retention policy, or long-term memory is introduced.

API and repository behavior is covered with controlled repository/connection doubles. Live PostgreSQL migration, paging, and transaction behavior remain unverified unless separately reported by a live database run.

## Core v0.8 — PostgreSQL Integration Verification (implemented; live verified)

- Add an opt-in live PostgreSQL integration suite using a password-free loopback admin URL, `PGPASSFILE`, PostgreSQL client tools, and a unique disposable `jarvis_it_<hex>` database.
- Exercise migration checksums/reruns, chat continuation/history pagination, missing conversations, rollback/recovery, concurrent appends, and sanitized connection failure.
- Keep the suite outside the default offline `tests/` collection; skipped live tests are never reported as database verification.

The local PostgreSQL 18 integration suite passed 11/11 tests (5 URL guard cases and 6 live database cases). The generated database was removed. See the verification note in `docs/HANDOFF.md` for the environment and remaining deployment limitations.

## Core v1.0 — Memory Retrieval Quality and Lifecycle (implemented)

- Define a small deterministic PostgreSQL corpus for lexical search regression tests. Assert explicitly relevant IDs, ranking/tie behavior, pagination, and no-match results; this is a regression fixture, not a general relevance benchmark.
- Specify duplicate, PATCH/null, timestamp, and permanent-delete semantics with API, repository, and opt-in PostgreSQL integration coverage.
- Emit metadata-only retrieval diagnostics for success, empty results, and fallback with execution ID, duration, and returned count. Never log queries, memory fields/IDs, credentials, or exception details.
- Preserve explicit API-only memory creation, retrieval-off-by-default, the five-record maximum, untrusted-context framing, and the existing public API contract.

No migration or dependency is planned. Authentication, per-user ownership, embeddings, pgvector, automatic extraction, and schedulers remain excluded; the memory API remains unsuitable for shared/untrusted deployments.

## Core v1.1 — Memory Management and User Control (implemented)

- Add exact category filtering to memory listing with filtered count and pagination semantics; reject blank categories.
- Add an accessible local memory manager for paginated inspection, lexical search, explicit create/edit, optional metadata clearing, and confirmed permanent deletion.
- Add IDs-only chat retrieval metadata for success, empty, fallback, and disabled outcomes; allow navigation to current records and gracefully report stale IDs without claiming a snapshot.
- Preserve unauthenticated/local-only warnings, opt-in retrieval, explicit-only memory creation, the five-record bound, untrusted context framing, and offline mock operation. No schema migration or dependency was required.

## Core v1.2 — Single-owner local authentication and memory isolation (implemented)

- Require a validated high-entropy bearer token and stable configured owner UUID when persistence is enabled; keep stateless mock mode available without credentials.
- Gate persisted chat, conversation history, and every memory route with one shared authentication dependency; scope memory reads, counts, search, updates, deletes, and retrieval by owner.
- Append migration 0004 with nullable owner ID and an owner listing index. Preserve legacy memories as unowned and inaccessible; no automatic claim or HTTP claim route.
- Add an in-memory-only frontend unlock/lock lifecycle and clear credentials on 401; retain clear local-only deployment warnings.

Persistence-enabled routes use the configured single-owner bearer token and memory SQL owner predicates; legacy memories remain unowned and inaccessible. One independent Gemini audit reported 15 PostgreSQL integration tests passed against isolated PostgreSQL 18 databases. A later offline verification recorded 5 URL-guard cases passing while 10 live database cases skipped because that run lacked opt-in configuration. These are separate historical runs; live PostgreSQL tests were not rerun during v1.3. SQL predicates are not row-level security, and a local bearer token does not make HTTP safe over untrusted networks.

## Core v1.3 — Registry consistency and orchestration contracts (implemented)

- Version the validated expert registry configuration and move deterministic phrase classification rules into it, preserving phrase matching order, current supported phrases, general-inquiry behavior, and classifier-failure fallback.
- Declare Analysis and Validation output contracts by allowlisted IDs in configuration. Resolve IDs only through a fixed Python mapping to existing Pydantic models; reject unknown, missing, duplicate, or persona-incompatible configuration at startup.
- Narrow the expert execution protocol to text or the two existing structured output models while retaining runtime Pydantic validation.
- Preserve the public API, provider selection, planner semantics, retry/timeout/partial behavior, memory, and empty non-executable tool registry. No migration or dependency was added.

Verification in this v1.3 worktree: `.venv-dev\Scripts\python.exe -m pytest -v tests/test_orchestration.py tests/test_api.py` passed 64 tests; `.venv-dev\Scripts\python.exe -m pytest -v` passed 164 tests; `.venv-dev\Scripts\python.exe -m compileall -q jarvis tests integration_tests` passed; and `git diff --check` passed. No live PostgreSQL or OpenAI request was run during v1.3. One Starlette TestClient deprecation warning remains.

## Core v1.4 — Deterministic evidence provenance (implemented)

- Keep Pydantic schema validation as the structural check. Add a separate deterministic checker for whether structured evidence text can be located in the original user request or another successful expert's summary.
- Normalize with Unicode NFKC, case-folding, and whitespace collapsing; retain punctuation and use normalized substring matching only. Exclude the target expert's own output, nested evidence, retrieved memories, failed output, and provider prompts as sources.
- Record internal outcomes as `source_match`, `unverified`, or `not_evaluated`; the internal response contains source identifiers/indexes only, and logs contain outcome counts only. Mock mode skips evaluation deterministically.
- Provenance does not establish that a source supports a claim, detect logical contradictions, or establish factual truth. No verifier persona, provider call, public API change, frontend work, migration, dependency, or external action is added.

## Core v1.5 — Shared retrieval and expert deadline (implemented)

- Establish one monotonic deadline at the chat route and propagate the same absolute value through optional memory retrieval, orchestration, and expert execution. Return `request_timeout` if it expires before expert execution; retain `provider_timeout` for expert/provider execution timeouts.
- Enforce PostgreSQL memory search budgets before connection and before each of its count/page queries with transaction-local, parameter-bound `set_config('statement_timeout', ..., true)`. Require at least one whole second for a connection attempt because libpq timeout granularity is seconds; this is not an exact total wall-clock guarantee.
- Preserve best-effort retrieval fallback while time remains, provider non-invocation after expiry, owner scoping, privacy-safe logs, retries, partial results, v1.4 provenance boundaries, and the public chat success schema. Persistence after synthesis remains outside the shared deadline.
- No migration, dependency, provider, frontend change, automatic memory behavior, or external action is introduced.

## Core v0.9 — Persistent Memory and Retrieval Foundation (implemented; verified locally)

- Add an incremental PostgreSQL memory migration and a separate memory repository protocol/adapter. Memory content is strictly bounded, explicitly created/updated/deleted, and never copied automatically from chat messages.
- Add bounded `/memories` create/list/get/patch/delete and deterministic PostgreSQL lexical full-text search. Search is not semantic and does not use embeddings or pgvector.
- Make chat retrieval explicitly configurable and disabled by default. Pass a small typed set of retrieved records as untrusted context; retrieval failures fall back to no memory context.
- Preserve offline mock startup and ordinary chat when persistence is disabled. Memory API operations return sanitized 503 when storage is unavailable.
- Document that the memory API is global and unauthenticated and must not be exposed to untrusted/shared deployments; UUIDs do not grant ownership or authorization.

Verification on Windows/Python 3.12.10: 143 default Python tests and 13 focused memory tests passed; the opt-in PostgreSQL integration suite passed 12/12 (5 URL checks and 7 live PostgreSQL cases); compileall, 16 frontend tests, the production build, and `git diff --check` passed. Live OpenAI behavior and semantic quality remain unverified. The milestone adds no account/ownership model, embedding generation, pgvector, automatic memory extraction, retention scheduler, memory audit history, or authentication/ownership controls.

## Later milestones

1. Identity and memory governance: authenticated users, explicit ownership, access controls, retention/export/revocation, and user-visible memory controls before exposing memory in shared deployments.
2. Real model adapters: optional local CPU model and compatible hosted providers behind provider interfaces, with explicit cost, privacy, timeout, and retry controls.
3. Safe tools and permissions: tool registry/runtime, permission and approval gateway, independent verification, bounded retries, and fault isolation.
4. User experience: React/TypeScript interface, natural conversation, voice input/output, and supported desktop/mobile integrations.
5. Personal knowledge: document ingestion, web research, semantic retrieval, citations, and personalized learning/tutoring, building on the explicit lexical memory foundation.
6. Personal workflows: task management, reminders, scheduling, and proactive monitoring/notifications.
7. Specialized capabilities: travel planning, creative tools, and smart-home integrations behind explicit user permissions.
8. High-risk domains: financial planning, expense analysis, stock-market research, and experimental forecasting with clear limitations and no transactions without explicit authorization.
9. Defensive operations: system health monitoring and defensive cybersecurity, with careful authorization and auditability.
10. Controlled improvement: model evaluation, fine-tuning experiments, versioning, and rollback with human review.

The long-term vision includes natural conversation and voice; desktop and supported mobile-device integrations; coding, debugging, test execution, fixes and independent verification; document analysis, web research, personal knowledge and long-term memory; task management, reminders, scheduling and personal workflows; financial planning, expense analysis, stock-market research and experimental forecasting; proactive monitoring and notifications; smart-home integrations; defensive cybersecurity and system health monitoring; personalized learning and tutoring; travel planning and creative tools; and controlled self-improvement, model evaluation, fine-tuning experiments and rollback.
