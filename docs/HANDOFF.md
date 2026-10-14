# JARVIS Handoff for the Next Coding Assistant

## Current milestone

Core v1.6 bounds synchronous PostgreSQL persistence operations with a separate monotonic operation budget. The v1.5 shared deadline still covers memory retrieval and expert execution only; persistence after synthesis has its own bound. Persistence and retrieval remain disabled by default, and mock chat does not need PostgreSQL. Memory records are separate from conversation history and are never automatically extracted from messages. All persisted API routes require configured bearer authentication. Apply migrations explicitly with the existing Windows migration runner; the app does not migrate at startup. This remains unsuitable for shared/public deployment.

## Implemented

- FastAPI `/health`, `/ready`, and `/chat` endpoints, validated configuration, request limits, sanitized errors, and structured logging.
- Vendor-independent provider protocol, deterministic offline mock provider, and optional OpenAI Chat Completions adapter with validated output, sanitized failures, explicit connect/read timeouts, and bounded transient retries through the orchestrator.
- Orchestration contracts for request context, intent, execution plan, expert output/result, audit events, and internal response.
- Safe deterministic intent classification with `general_inquiry`/confidence `0.0` fallback on classifier failure.
- Pydantic-validated expert and tool registry configuration under `jarvis/orchestration/config/`. The seven configured personas route to the existing shared provider; they are not separate reasoning agents.
- Bounded planner (maximum five rounds), sequential by default. Parallel execution requires an explicitly configured intent and uses a semaphore bounded by settings.
- Pydantic output validation, explicit-transient-only exponential retries within expert execution, dependency-aware execution, and partial synthesis when successful outputs remain. The shared route deadline across retrieval and expert execution was added in Core v1.5 below.
- A separate deterministic provenance checker locates structured evidence text in the original user request or another successful expert's summary. It uses NFKC normalization, case folding, whitespace collapsing, retained punctuation, and exact normalized substring matching, with bounded evidence and comparison work. It excludes self-output, nested evidence, retrieved memories, failed output, and provider prompts. It reports only `source_match`, `unverified`, or `not_evaluated`; mock mode deterministically skips evaluation. This does not establish support, logical consistency, or factual truth.
- Metadata-only logs/audit-event schemas; message text, provider output, exception text, and credentials are excluded. Audit events are in-memory only.
- The `/chat` response retains `reply`, `provider`, and `demo` and adds a `partial` boolean. Frontend renders a partial-result label when that value is true. Combined expert output is clipped at 40,000 characters with a visible truncation marker; clipping does not set `partial`.
- Chat success, provider failure, and timeout responses carry an `X-JARVIS-Execution-ID` header that correlates with safe structured log and audit metadata. `provider` and `demo` identify the selected response source without changing the response contract.
- AnalysisExpert and ValidationExpert have separate configured system instructions. Regression tests verify that each instruction reaches OpenAI request payloads through mocked HTTP. They still share one provider adapter; the mock does not perform model reasoning.
- AnalysisExpert and ValidationExpert use bounded Pydantic structured-output contracts. The OpenAI adapter requests strict Chat Completions JSON Schema after transforming a copy to remove string/array size bounds and documented unsupported composition keywords; local Pydantic validation retains all original length, collection, required-field, and semantic checks. The OpenAI guide marks `maxItems` as supported for standard models but not fine-tuned models, so all array bounds are omitted for model-selection compatibility. The adapter validates strict structure, object closure, required properties, and local references before sending, and fails sanitized if any feature remains unsupported. Mocked requests verify schema payload construction, not live endpoint compatibility. Truncated structured completions fail permanently. Structured calls are bounded to 8,192 completion tokens; ordinary text calls remain at 1,024. Mock results preserve the historical deterministic response and are marked `not_evaluated` with empty specialist fields. Shape validation does not establish semantic accuracy.
- Provenance checking is a separate exact normalized substring check; it does not duplicate Pydantic schema validation or claim evidence support, logical consistency, or factual truth. It runs after successful expert execution and remains internal to the orchestration response. Only bounded outcome counts are logged. Mock mode does not evaluate evidence, and checker failures fall back to `not_evaluated` without discarding successful or partial results.
- OpenAI refusal responses and malformed, empty, or whitespace-only completion content are sanitized permanent provider failures; no retry or mock fallback occurs.
- Tool configuration is currently empty, the registry has no executor, and no shell command or external/state-changing action can be invoked.
- Standalone Windows PostgreSQL provisioning scripts remain optional and outside application startup.
- Optional `jarvis.persistence` provides a small repository protocol and PostgreSQL adapter. The `persistence` extra declares `psycopg[binary]`; set `JARVIS_PERSISTENCE_ENABLED=true` and a secret `JARVIS_PERSISTENCE_DATABASE_URL` only in a local environment. `/chat` returns sanitized HTTP 503 if saving fails. The same response shape is retained on success.
- `JARVIS_PERSISTENCE_OPERATION_TIMEOUT_SECONDS` defaults to 5 seconds and accepts 1–60. It separately bounds conversation and memory adapter calls. SQL statements use transaction-local timeouts recalculated from the remaining budget; libpq connection timeout is whole-second granularity and cannot guarantee an exact wall-clock deadline. SQL failures roll back before translation to the existing sanitized 503 behavior.
- `/chat` accepts an optional UUID `conversation_id`. Persistent responses return the created or continued ID; non-persistent responses return `null`. `GET /conversations/{conversation_id}/messages` accepts `limit` (1–100, default 50) and `before_sequence` for older pages. Malformed IDs return sanitized 422, unknown IDs return sanitized 404, and unavailable persistence returns sanitized 503.

## Commands

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m uvicorn jarvis.main:app --reload
python -m pytest -v
```

The project uses the declared `setuptools.build_meta` PEP 517 backend with `setuptools>=68`. When online, pip's isolated build environment can install that requirement during `pip install -e ".[dev]"`. An offline install requires setuptools>=68 and the declared project packages to be present in the build environment or local wheel cache. Wheel packaging has not been verified.

Frontend checks from `frontend/`:

```powershell
npm test -- --run
npm run build
```

## Current limits

- Intent classification and planning are deterministic, not model-driven. The configured personas share one provider; AnalysisExpert and ValidationExpert have distinct model instructions and structured contracts. In mock mode the instructions are ignored and responses are deterministic demo text with `not_evaluated` structured placeholders, not model reasoning.
- The only configured tool list is empty. Tool execution, permission/approval gateway, persistent audit storage, and external integrations are not implemented. Conversation persistence is opt-in and gated by one configured bearer token. Core v1.2 scopes explicit memory CRUD/search to a configured owner; it does not add multi-user accounts, PostgreSQL row-level security, semantic retrieval, embeddings, pgvector, automatic extraction, retention scheduling, or memory audit history. Core v1.1 adds a local memory-management interface, updated in v1.2 with in-memory credential handling.
- `/ready` confirms successful settings/provider/orchestrator construction; it does not probe external systems.
- The default provider runs without a GPU, API key, or internet. Frontend conversation state is in-memory only.
- OpenAI selection requires `JARVIS_OPENAI_API_KEY`; setting it does not fall back to mock if calls fail. Requests send user message content to the configured endpoint and may incur charges. No live provider call was performed during tests.
- Analysis and validation require an endpoint/model that supports strict Chat Completions JSON Schema output. Unsupported-schema behavior is verified using mocked HTTP responses; live compatibility, model quality, and network connectivity remain unverified.
- Provider cancellation is performed by `asyncio.wait_for`; future asynchronous adapters must release resources when cancelled.
- The PostgreSQL foundation and its restore drill are documented in `database/README.md`; operational use still requires confirming the user's local server configuration and restore target.
- Persistence can link multiple exchanges with a client-supplied conversation ID and expose bounded history pages. A single configured local bearer credential gates all persistent operations; conversation IDs remain identifiers, not authorization. Memory ownership is enforced by application SQL predicates only. Keep deployment local and trusted.

## Verification on this working tree

Core v0.7 verification on Windows/Python 3.12.10: `.venv-dev\Scripts\python.exe -m pytest -v` passed 130 tests; `.venv-dev\Scripts\python.exe -m compileall -q jarvis tests` passed; `npm test -- --run` passed 16 frontend tests; `npm run build` succeeded; and `git diff --check` passed. Python tests ran with loopback access because restricted sandbox socket initialization stalls FastAPI TestClient. API and repository paging/transaction behavior used controlled doubles. No live PostgreSQL migration or adapter test was run; the `psycopg` package and `psql` executable were unavailable.


- `.venv-dev\Scripts\python.exe -m pytest -v`: 101 passed in 1.42s when run with sandbox restrictions lifted.
- `.venv-dev\Scripts\python.exe -m pytest tests/test_intelligence_provider.py -q`: 44 passed in 0.77s when run with sandbox restrictions lifted.
- The earlier sandbox hang was traced with a bounded faulthandler dump to Windows asyncio Proactor and Selector loop startup blocking in Python's `socket.py::_fallback_socketpair`, at local `accept()`. A local `socket.socketpair()` and `asyncio.run(asyncio.sleep(0))` work with sandbox restrictions lifted. This was a sandbox loopback restriction, not a JARVIS production-code defect. Run Python tests in an environment that permits loopback socket creation.
- Python is 3.12.10 and the active `.venv-dev` pytest is 8.4.2, within the declared `<9.0` range. Starlette emits an existing TestClient deprecation warning recommending `httpx2`; that package is not installed or declared. Dependencies were left unchanged. The earlier sandbox hang is traced to restricted loopback socketpair initialization, not a JARVIS production-code defect.
- `npm test -- --run` from `frontend/`: 16 passed.
- `npm run build` from `frontend/`: passed.
- `.venv-dev\Scripts\python.exe -m compileall -q jarvis tests`: passed.
- `git diff --check`: passed.
- OpenAI request/refusal/error tests use mocked HTTP transport; no live provider call was made.

## Core v0.8 PostgreSQL verification review

The v0.8 review adds an opt-in live PostgreSQL test module at `integration_tests/test_postgres_integration.py`. It is outside the default `tests/` collection and requires explicit opt-in, the optional `persistence` extra, PostgreSQL client tools, a password-free admin URL restricted to loopback, and a protected `PGPASSFILE`. The fixture provisions a randomly named disposable integration database and cleanup targets only that generated name. The module covers migrations/checksums/reruns, chat continuation/history paging, unknown IDs, transaction rollback/recovery, concurrent appends, and sanitized connection failure. Setup and exact PowerShell commands are documented in `database/README.md`.

Verification during this review: the full default Python suite passed 130 tests; the focused persistence/database-runner suite passed 29 tests; `compileall` completed successfully; all 11 opt-in integration tests passed, including all 6 live database cases against the local PostgreSQL 18 server. Frontend tests passed 16 and the production build succeeded. `git diff --check` passed. The generated integration database was removed, and a read-only post-run query found no remaining generated integration databases. The fixture checks that `PGPASSFILE` exists but does not enforce its Windows ACL; the operator verified the local file ACL separately. The `httpx`/Starlette TestClient deprecation warning recommending `httpx2` remains; dependency changes are outside this database verification review.

## Core v0.9 memory verification

Memory is explicit and distinct from conversation history. The new memory API stores bounded records and offers PostgreSQL lexical full-text search; it does not provide semantic/vector retrieval, embeddings, automatic extraction, per-user isolation, or retention scheduling. Retrieval is off by default, bounded to at most five records when enabled, passed as untrusted context, and falls back to no memory context on storage errors. Memory API calls return sanitized 503 when persistence is disabled. The memory API has no authentication or ownership enforcement and must not be exposed to untrusted/shared deployments.

Lifecycle contract: duplicate content creates separate IDs; PATCH changes only supplied fields, optional category/source values can be cleared with `null`, content cannot be cleared, and empty patches are rejected. Every successful patch refreshes `updated_at`, including same-value patches. Delete is permanent; repeated delete returns 404. Retrieval diagnostics include only execution ID, outcome (`success`, `empty`, or `fallback`), duration, and result count. Query text, memory data/identifiers, categories, source references, credentials, and exception details are excluded.

Verification on Windows/Python 3.12.10: the full Python suite passed 143 tests; the focused memory suite passed 13 tests; the Core v0.9 opt-in PostgreSQL integration suite passed 12 tests (5 URL guard tests and 7 database-backed tests) against local PostgreSQL 18. `compileall` passed for `jarvis`, `tests`, and `integration_tests`; the frontend suite passed 16 tests and the production build succeeded. `git diff --check` passed. The generated integration database was removed. The live database test uses test-only memory content; no user memory content or credentials were committed. The `httpx`/Starlette deprecation warning remains.

## Next work

Core v1.0 completed lexical ranking regression coverage, duplicate/PATCH/delete semantics, and privacy-safe retrieval outcome diagnostics. Keep memory local/trusted: authentication and per-user ownership are still absent. Live OpenAI connectivity, strict-schema compatibility against a live endpoint, and semantic model quality remain unverified. Preserve the roadmap's longer-term goals and do not infer that future tools, embeddings, model-driven planning, or actions exist from current configuration interfaces.

## Core v1.1 — Memory management and chat transparency

The local React UI now includes a memory manager backed by the existing explicit CRUD and lexical search API. It supports paginated listing, exact category filtering, search, manual creation, editing (including clearing optional category/source fields), and confirmed permanent deletion. The screen explains that the API is unauthenticated and has no per-user ownership; keep it restricted to a trusted local environment. Persistence-disabled responses are surfaced as a clear setup message.

When chat retrieval is enabled, `/chat` adds `memory_context` with an outcome (`success`, `empty`, or `fallback`) and IDs of records actually supplied to the provider. With retrieval disabled the field is `null`. The UI can inspect those IDs in the manager and handles records that have since been deleted; it does not claim to show a historical snapshot. Memory content, IDs, query text, and metadata remain excluded from retrieval diagnostic logs. No migration or new dependency was added. Live PostgreSQL integration and live model behavior require the existing opt-in setup and were not inferred from offline tests.


Core v1.1 verification on this working tree: full offline Python suite 147 passed; focused memory suite 17 passed; compileall passed; frontend suite 25 passed; production build and git diff --check passed. Live PostgreSQL integration tests were not run because opt-in environment variables were unset, and no live OpenAI request was made. The Starlette TestClient deprecation warning recommending httpx2 remains.

## Core v1.2 — Single-owner authentication and memory isolation (implemented)

Persistence-enabled startup now validates `JARVIS_LOCAL_AUTH_TOKEN` (at least 32 visible ASCII characters) and stable, non-nil `JARVIS_OWNER_ID`. One shared Bearer dependency gates all persisted `/chat`, `/conversations/*`, and `/memories/*` requests. Migration 0004 adds nullable ownership without a default or legacy backfill; unowned rows stay inaccessible and no HTTP claim endpoint exists. Memory SQL scopes reads, counts, search, updates and deletes to the configured owner. The React UI keeps credentials in memory, provides Unlock/Lock controls, and clears them on 401. Application predicates are not database row-level security; plain HTTP is unsafe over untrusted networks, XSS may read the active token, and this remains a local single-owner prototype, not safe for public/shared use.

Offline v1.2 verification: the full Python suite passed 157 tests; focused API/memory tests passed 48; compileall passed for `jarvis`, `tests`, and `integration_tests`; 30 frontend tests and the production build passed; and `git diff --check` passed. That offline integration-module run had 5 URL-guard tests pass and 10 live database tests skip because opt-in credentials/configuration and PostgreSQL client tools were unavailable. Separately, an independent Gemini audit reported 15 PostgreSQL integration tests passed against isolated PostgreSQL 18 databases. The latter is historical audit evidence, not a run performed during v1.3. No live OpenAI request was made.

## Core v1.3 — Registry consistency and orchestration contracts

The version-2 expert registry now contains the ordered phrase rules used by deterministic classification. Existing phrase precedence and fallback behavior are preserved. Analysis and Validation experts declare `analysis_v1` and `validation_v1`; the IDs are constrained by configuration validation and map through a fixed Python allowlist to the existing Pydantic models. No dynamic imports or plugin loading are used. The `Expert` protocol now declares the existing supported output union (`str`, `AnalysisExpertOutput`, or `ValidationExpertOutput`), while the executor retains runtime Pydantic validation. The Mother Orchestrator, Planner, response synthesis, provider selection, and execution policy were not rebuilt. `tools.json` remains empty and there is no tool executor.

v1.3 verification: `.venv-dev\Scripts\python.exe -m pytest -v tests/test_orchestration.py tests/test_api.py` passed 64 tests; `.venv-dev\Scripts\python.exe -m pytest -v` passed 164; `.venv-dev\Scripts\python.exe -m compileall -q jarvis tests integration_tests` passed; and `git diff --check` passed. No live PostgreSQL or OpenAI request was performed during v1.3. The Starlette TestClient deprecation warning recommending `httpx2` remains. At v1.3, the timeout bounded expert execution only; Core v1.5 changes the request boundary as described below.

## Core v1.4 — Deterministic evidence provenance

Evidence strings in structured analysis/validation output are checked against the original request and summaries from other successful experts. Matching uses NFKC, case-folding, collapsed whitespace, retained punctuation, and substring matching. The target expert's own output, evidence fields, retrieved memories, failed results, and provider prompts are excluded. Work is bounded by source/evidence length and result/target counts; the 10,000,000-character work budget includes normalization input/output and comparisons. The request is checked first; if it cannot be normalized or compared within the remaining budget, the outcome is `unverified` and peer sources are not searched. Peer summaries use result order, with duplicate expert IDs excluded as ambiguous. Missing or `None` evidence collections produce no evidence entries, while explicit malformed collection items are reported as `unverified` if representable. Outcomes and source identifiers remain internal; logs contain outcome counts only. This is provenance only; a located string may be false, irrelevant, or model-generated, and no logical contradiction or factual truth is established.

Initial v1.4 implementation verification: focused provenance/orchestration/API tests passed 76; the complete offline Python suite passed 176; compileall and `git diff --check` passed. One existing Starlette TestClient deprecation warning remains. Live PostgreSQL integration tests were not run, and no live OpenAI request was made.

## Core v1.5 — Shared retrieval and expert deadline

`/chat` establishes one absolute monotonic deadline and passes it through optional memory retrieval, orchestration, and expert execution. Expiry during retrieval returns sanitized HTTP 504 `request_timeout` with the existing execution-ID header and does not call a provider. Retrieval failures before expiry remain best-effort `fallback`; provider/expert timeouts remain `provider_timeout`. The public success response contract, authentication/owner scoping, expert retry and partial-result rules, and v1.4 provenance boundaries are unchanged. Persistence after synthesis is outside this deadline.

PostgreSQL memory search checks the budget before opening a connection and refuses to connect with less than one whole second remaining. libpq connection timeouts have whole-second granularity; the positive timeout is `min(configured_timeout, floor(remaining_seconds))`. Flooring can shorten or reject an attempt and cannot guarantee an exact sub-second wall-clock deadline. Before both search queries the adapter recalculates remaining time and uses bound `SELECT set_config('statement_timeout', %s, true)` inside the transaction. Transaction commit/rollback resets the setting. The route does not rely on cancelling a threadpool await to stop synchronous database work; PostgreSQL statement/connection timeouts bound those operations. Live PostgreSQL verification is opt-in and must be reported separately from offline tests.

## Core v1.6 — Bounded PostgreSQL persistence operations

All synchronous conversation and memory repository operations use a separate `JARVIS_PERSISTENCE_OPERATION_TIMEOUT_SECONDS` budget (default 5 seconds, range 1–60). PostgreSQL statements receive transaction-local timeouts based on remaining operation time; transaction completion resets the setting, and timeout failures roll back before sanitized translation. The v1.5 retrieval/expert budget, API response contracts, authentication, owner scoping, and mock/offline mode remain unchanged. No schema migration or dependency was added. Operation budgets are not exact end-to-end wall-clock guarantees because libpq connection timeouts use whole seconds and database/client overhead remains.

The following v1.6 verification results were supplied as completed terminal output by the repository operator; they were not rerun during this documentation and commit task. Earlier milestone notes above are historical snapshots and do not describe the current v1.6 verification state.

## Current v1.6 verification

- Full Python suite: 219 passed, 1 existing Starlette/httpx TestClient deprecation warning.
- Opt-in PostgreSQL integration suite: 18 passed in 18.47 seconds, with 1 existing Starlette/httpx TestClient deprecation warning. The run included `test_conversation_statement_timeout_rolls_back_and_connection_recovers` against the configured live PostgreSQL test environment.
- Frontend suite: 30 passed; frontend production build, Python compilation, and `git diff --check` passed.
- Gemini independent audit: APPROVE WITH CONDITIONS; the required live PostgreSQL verification condition was subsequently satisfied.
- No live OpenAI request was made. These are operator-supplied results, not commands rerun during this documentation/commit task.

## Core v1.7 Stage 2 — Retrieval evaluation handoff

The current uncommitted Stage 2 change is test and documentation only. `integration_tests/test_postgres_integration.py::test_memory_lexical_retrieval_evaluation` retains the 12 Stage 1 current-ranking assertions and measures three deterministic synthetic length-sensitive queries against current `ts_rank_cd` normalization 0 and a normalization 1 candidate. Stable UUIDs/timestamps and owner predicates are used; both rankings are repeated to check determinism, and structured output records per-query order, rank scores, and Precision@5, Recall@5, and MRR@5.

The observed synthetic means were Precision@5 0.2 and Recall@5 1.0 for both candidates; mean MRR@5 was 0.3333 current versus 1.0 candidate, a 0.6667 gain. The candidate nevertheless reordered the Stage 1 `common` query's expected top five, so it is rejected under the no-regression criterion. Do not apply normalization 1 to production ranking based on this evaluation. Results are limited to the synthetic test corpus and do not establish general retrieval quality. Production SQL, API behavior, schema, migrations, dependencies, frontend, retrieval defaults, ownership, deadlines, and untrusted-context handling are unchanged.

Verification observed for this Stage 2 working tree: the targeted opt-in PostgreSQL test passed against the fixture-generated disposable database; the focused offline memory suite passed 26 tests; compileall for `jarvis`, `tests`, and `integration_tests` passed; and `git diff --check` passed. The test run reported one existing Starlette/httpx TestClient deprecation warning. A read-only post-run metadata query confirmed the generated database had been removed. No live OpenAI request was made. The work remains uncommitted.
