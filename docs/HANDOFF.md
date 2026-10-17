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
- The only configured tool list is empty. Tool execution, approval workflow, persistent audit storage, and external integrations are not implemented. Core v1.8 adds a small server-side capability policy for existing single-owner operations; it does not add per-user grants, multi-user accounts, PostgreSQL row-level security, semantic retrieval, embeddings, pgvector, automatic extraction, retention scheduling, or memory audit history. Conversation persistence is opt-in and gated by one configured bearer token. Core v1.1 adds a local memory-management interface, updated in v1.2 with in-memory credential handling.
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

Stage 2 was committed locally as `aa735e60ef226135e3b29da0612e2e95bec4299b`. The working tree was clean at the verified Stage 3 starting state. The commit has not been pushed. `integration_tests/test_postgres_integration.py::test_memory_lexical_retrieval_evaluation` retains the 12 Stage 1 current-ranking assertions and measures three deterministic synthetic length-sensitive queries against current `ts_rank_cd` normalization 0 and a normalization 1 candidate. Stable UUIDs/timestamps and owner predicates are used; both rankings are repeated to check determinism, and structured output records per-query order, rank scores, and Precision@5, Recall@5, and MRR@5.

The observed synthetic means were Precision@5 0.2 and Recall@5 1.0 for both candidates; mean MRR@5 was 0.3333 current versus 1.0 candidate, a 0.6667 gain. The candidate nevertheless reordered the Stage 1 `common` query's expected top five, so it is rejected under the no-regression criterion. Do not apply normalization 1 to production ranking based on this evaluation. Results are limited to the synthetic test corpus and do not establish general retrieval quality. Production SQL, API behavior, schema, migrations, dependencies, frontend, retrieval defaults, ownership, deadlines, and untrusted-context handling are unchanged.

Verification recorded for Stage 2: the targeted opt-in PostgreSQL test passed against the fixture-generated disposable database; the focused offline memory suite passed 26 tests; compileall for `jarvis`, `tests`, and `integration_tests` passed; and `git diff --check` passed. The test run reported one existing Starlette/httpx TestClient deprecation warning. A read-only post-run metadata query confirmed the generated database had been removed. No live OpenAI request was made. These results belong to the locally committed Stage 2 change; no push was reported.

## Core v1.7 Stage 3 — Normalization 2 evaluation (operator-reported)

The operator-provided live evaluation output reports 1 passed against generated database `jarvis_it_9c3e12a1dba645e3bf602ff6b5889d3d`. For each of the three synthetic length-sensitive queries, normalization 2 reportedly achieved MRR@5 1.0 versus 0.3333 for current normalization 0; mean MRR@5 improvement was 0.6667. Precision@5 (0.20) and Recall@5 (1.00) were unchanged. The candidate regressed the Stage 1 `common` ranking and was rejected by the no-regression gate despite its targeted MRR improvement. Production ranking remains unchanged. These results come from a small synthetic corpus and do not establish general retrieval quality.

The fixture checks for a generated-name collision before creating a database and only attempts cleanup after it has marked its generated database as created. After `DROP DATABASE` returns, it now queries the `postgres` catalog on the maintenance connection and reports a sanitized cleanup-verification failure if that generated name remains. This added check does not independently verify cleanup of the database reported in the earlier Stage 3 run; do not treat the earlier test pass as proof of cleanup. The offline memory suite was previously reported as 26 passed with one existing Starlette/httpx TestClient deprecation warning; that result was not rerun for this handoff update. The Stage 3 live result and offline result above are operator-reported, not independently executed during this documentation review.

Additional operator-reported verification for the current Stage 3 working tree: the PostgreSQL integration suite passed 19 tests, `tests/test_database_runner.py` passed 12 tests, and `git diff --check` reported no whitespace errors (Git emitted LF-to-CRLF warnings). The operator reports that the 19-test PostgreSQL run used the fixture with post-drop cleanup verification present; this execution order is reported but cannot be independently established from repository artifacts, and the tests were not rerun during this task. On that report, the passing suite exercised successful drop and catalog-absence verification for its generated database. It does not independently test cleanup failure paths or establish behavior for every cleanup scenario.

## v1.7 — Bounded document ingestion (implemented; latest live result operator-reported)

This increment adds one explicit authenticated `POST /memories/ingest` operation for a `.txt` or `.md` file, creating exactly one existing owner-scoped memory record. The server streams a maximum of 16 KiB, strictly decodes UTF-8, strips one leading BOM, and enforces the existing 4,000-character ceiling before writing. It rejects malformed UTF-8, NUL, blank content, oversized input, and unsupported extensions. `source_reference` receives only a sanitized basename; repeated imports create distinct records. The frontend displays the selected filename in a confirmation dialog before upload. Imported Markdown is stored as text, not interpreted; links are not followed, and code or instructions are not executed. No migration, dependency, production retrieval/ranking change, or automatic memory saving is included.

Earlier implementation verification (operator-reported): `.venv-dev\Scripts\python.exe -m pytest -v` passed 245 tests with one existing Starlette/httpx deprecation warning. The process exited 0; pytest also emitted an ignored Windows `PermissionError` from its temporary-directory cleanup callback at interpreter exit. Compileall passed; frontend tests passed 33 tests; the frontend build passed; and `git diff --check` passed with LF-to-CRLF warnings. This is an earlier implementation snapshot, not the latest offline result.

Earlier live PostgreSQL verification (operator-reported): the integration suite passed 20 tests with 0 failures against a working tree that included cleanup-verification code. It was not rerun after the later filename-validation and documentation fixes, so it does not verify the latest working tree. The reported pass exercises the fixture's normal post-drop absence check; it does not establish that every possible cleanup failure scenario has been tested.

Later verification after the filename-validation and documentation fixes (operator-reported): the full Python suite passed 250 tests; Python compilation and `git diff --check` passed. These results were reported by the operator and were not independently reproduced in that review. PostgreSQL integration tests were not rerun after those fixes. No live OpenAI request was made.

Follow-up verification for the Windows drive-relative filename fix (run during this task): `.venv-dev\Scripts\python.exe -m pytest -q tests/test_memory_ingestion.py` passed 21 tests; `.venv-dev\Scripts\python.exe -m pytest -q tests/test_api.py -k document_ingestion` passed 12 with 31 deselected; and `.venv-dev\Scripts\python.exe -m pytest -q` passed 252 tests with one existing Starlette/httpx deprecation warning. The full-suite process exited 0, followed by an ignored Windows `PermissionError` from pytest's temporary-directory cleanup callback at interpreter exit. `.venv-dev\Scripts\python.exe -m compileall -q jarvis tests integration_tests` and `git diff --check` passed; Git emitted LF-to-CRLF working-copy warnings. PostgreSQL integration tests were not rerun after the latest filename fix. Frontend tests/build were not rerun because this follow-up did not change frontend files.

## Core v1.8 — Centralized authorization policy foundation

Add server-side capability checks after the existing single-owner bearer authentication for advisory chat, memory reads/writes, and conversation reads/writes. Stateless mock chat remains unauthenticated only when persistence is disabled. Unknown capabilities, missing owners, missing policy, and indeterminate policy decisions fail closed; external action execution is not granted. The policy does not treat intent labels, expert output, plans, or tool metadata as authorization. Approval records/APIs/UI, multi-user grants, migrations, and any executor remain deferred. The configured owner receives the existing operation set, so this establishes explicit enforcement points but not granular human identities or two-person approval.

## Core v1.9 — Integration, security, and workflow verification (implementation in review)

The implementation requires the configured single-owner bearer identity for `/chat` when the server-selected provider is non-demo, even if persistence is disabled. Anonymous stateless chat remains available only for the deterministic demo provider. This uses the existing bearer token and owner UUID as an authenticated policy principal; selecting a provider in request data cannot bypass the boundary. When persistence is enabled, existing authentication, route policy checks, and memory owner predicates remain in force.

HTTP denial tests exercise valid requests for memory ingestion, search, get, create, update, and delete, assert sanitized 403 responses, and prove repository methods are not reached. A deterministic API-level workflow uses the existing app/orchestration service/mock provider with in-memory test repositories to cover document import, stored owner association, owner-scoped lexical search, retrieved-memory context at the provider boundary, and ordered conversation history. A rejected document import verifies no partial memory write. This is not browser-level E2E and does not replace PostgreSQL integration testing.

The opt-in PostgreSQL fixture now performs read-only detection/reporting of pre-existing names matching the strict generated `jarvis_it_<32 lowercase hex>` convention. It distinguishes observed active connections from inactive/uncertain entries and never automatically removes pre-existing databases. Connection absence cannot prove an interrupted run is stale or rule out an active process between connections; manual review is required. Normal fixture teardown remains restricted to the generated database created by that fixture and retains post-drop catalog verification.

Implementation and development verification are not independent QA or owner acceptance. The tool registry remains empty and `execution_enabled` remains false; the system is advisory-only, with approvals and external actions deferred. This is still a local single-owner application, not safe for shared/public deployment. The project owner subsequently approved v2.0 design and implementation phases; the current v2.0 status is recorded below.

## Core v2.0 — Integrated local platform handoff (Phase 6)

### Status

Phases 1–6 are implemented in the current working tree. Phase 1 (conversation summary contract/repository) and Phase 2 (authenticated conversation list) were reported independently reviewed. Phase 3 added and reviewed PostgreSQL integration coverage. The latest live PostgreSQL result is **owner-reported: 27 passed, 1 warning in 25.73 seconds**. The project owner reports manually running the suite in PowerShell; the output showed 27 passing tests. The warning was a `StarletteDeprecationWarning` concerning `httpx` and `starlette.testclient`, not a test failure. The final read-only Codex review did not rerun the tests; this is owner-provided execution evidence, not an independently reproduced result from that review. The supplied summary does not independently establish the generated database's post-run absence. Phase 4's frontend audit was reported as PASS. Phase 5's Playwright audit was reported as PASS WITH FINDINGS; nine browser scenarios and 48 frontend unit tests were reported passing. Final v2.0 acceptance remains pending owner review.

### Implemented capabilities

- **Conversation summaries:** authenticated `GET /conversations?limit=20&offset=0`, guarded by the configured bearer identity and `conversation.read`. The response contains `items`, `limit`, `offset`, and `has_more`; each item contains `conversation_id`, `created_at`, `last_activity_at`, and nullable `preview`. Limit is 1–100 (default 20); offset is 0–1,000,000 (default 0). Ordering is activity time descending, then conversation UUID descending. Offset pages may shift as records change. Preview is the first user message, at most 120 Unicode characters; longer text is truncated to 119 characters plus `…`. Preview content must not be logged. Persistence-disabled/unavailable responses use the existing sanitized 503 behavior; authentication and policy failures retain 401/403 conventions.
- **History and UI:** history is loaded for display/navigation only and is not added to model prompts. The React app retains backend conversation IDs, lists saved conversations in pages, reopens history, and sends follow-up messages with the selected ID. Credentials exist in memory only. Lock and HTTP 401 clear credentials, drafts, selected conversations, previews, and displayed history. Aborted/versioned requests prevent stale responses from restoring cleared or differently selected state. Ambiguous requests are not automatically retried; the UI warns that manual retry can duplicate an exchange if the server saved it before the response was lost.
- **Existing memory controls:** v1.7 single-file `.txt`/`.md` ingestion remains limited to 16 KiB input and 4,000 decoded characters, with strict UTF-8 and one leading BOM stripped, validation before persistence, and a sanitized basename source reference. Existing lexical memory retrieval remains disabled by default, owner-scoped, capped at five, deadline-aware, and framed as untrusted. The rejected `ts_rank_cd` normalization experiment has not changed production ranking.
- **Policy and actions:** the v1.8 policy is an explicit server-side allowlist for the configured single owner, not multi-user RBAC. It does not add approval records or execution authority. The tool registry remains empty and `execution_enabled=False`; external actions are disabled.

### Browser and integration test boundaries

`npm run test:e2e` uses `frontend/playwright.config.mjs` to start the API and Vite on loopback with mock provider selected, persistence disabled, retrieval disabled, and existing server reuse prohibited. The nine scenarios cover app startup, anonymous stateless mock chat, synthetic authenticated conversation listing/history and follow-up, empty state, lock/401 clearing, stale responses, safe errors/manual retry, and memory-import confirmation. Some conversation API responses are intercepted and synthetic; those browser cases do not verify the database or prove PostgreSQL persistence. The dedicated opt-in PostgreSQL suite is the only live database coverage for persisted conversation listing.

### Verification performed during this Phase 6 run

- `D:\Project_Jarvis\.venv-dev\Scripts\python.exe -m pytest -v`: **306 passed**, 1 existing Starlette/httpx TestClient deprecation warning. Pytest also printed an ignored Windows `PermissionError` in its temp-directory cleanup callback after the passing summary.
- `D:\Project_Jarvis\.venv-dev\Scripts\python.exe -m compileall -q jarvis tests integration_tests`: passed.
- `npm test -- --run`: **48 passed** across 3 frontend test files.
- `npm run build`: passed (`tsc -b` and Vite production build).
- `npm run test:e2e`: **9 passed**. Node emitted the warning that `NO_COLOR` is ignored while `FORCE_COLOR` is set.
- `git diff --check`: passed with no whitespace errors; Git emitted LF-to-CRLF working-copy warnings for changed files.
- No live PostgreSQL integration test, migration, database connection, or external model request was performed in Phase 6. `pyproject.toml` limits the default pytest discovery to `tests/`; the live suite is separately located under `integration_tests/` and is opt-in.

### Outstanding gates and limitations

1. The owner-reported live PostgreSQL run satisfies the execution gate as reported. Before final acceptance, retain the cleanup-evidence limitation: the final read-only review did not rerun the suite or independently query the generated database's post-run absence. The fixture must continue to use its verified loopback, protected-credential, generated-disposable-database safeguards and preserve collisions rather than migrate or drop them.
2. Review and accept the v2.0 milestone after that gate. No future milestone is approved by this handoff.
3. No live OpenAI/provider verification is claimed. The platform remains local and single-owner; application SQL owner predicates are not database row-level security. It is not approved for shared/public deployment. No production-readiness claim is made.

**Phase 6 status: documentation and offline/browser regression run complete; live PostgreSQL execution is owner-reported as 27 passed with 1 warning in 25.73 seconds; final v2.0 acceptance remains pending project-owner review and the stated cleanup-evidence limitation.** No commit or push has been performed during this task.

## Core v2.1 — Reliability and operational readiness (implementation in progress)

### Gap and change

The previous `/ready` handler returned `ready` unconditionally after application construction. It could not identify missing in-process provider, orchestration, authorization-policy, or enabled persistence-repository wiring. `/health` remains a separate liveness signal. The v2.1 change adds fixed, sanitized component statuses to `/ready` and returns HTTP 503 when any required component is unavailable. It reports intentionally disabled persistence as `disabled` and enabled persistence as `configured` only when both repository adapters are present. It does not attempt a database connection and does not claim PostgreSQL is healthy. The response does not expose provider identity, URLs, credentials, prompts, content, or exception details.

### Verification in this v2.1 task

- Initial state observed: branch `main`, HEAD `aa735e60ef226135e3b29da0612e2e95bec4299b`, 3 commits ahead of `origin/main`, no staged changes, and accumulated changes from earlier milestones. The task preserved them.
- `.venv-dev\Scripts\python.exe -m pytest -vv -s tests/test_api.py::test_health_and_readiness_include_version` did not complete; a 45-second watchdog timed out after the test began. A direct invocation with `faulthandler` showed it blocked while `TestClient.__enter__` initialized a Windows asyncio Proactor event loop, before a request reached JARVIS. A separate local socket probe was denied with `PermissionError` (errno 13, WinError 10013). This is evidence of the current execution environment restricting loopback socket setup, not evidence of an application route or shutdown defect.
- The equivalent in-process Uvicorn smoke check, configured explicitly for mock provider and persistence disabled, received no response within 12 seconds and terminated its child process. No database or external provider was used. The standard E2E ports had no leftover listener after diagnostics.
- New pure readiness tests are to be reported below only after execution. The full Python suite was not run because the first API smoke test is blocked by the loopback restriction.
- `npm test -- --run src/App.test.tsx`: 26 passed in 20.05 seconds. `npm test -- --run`: 48 passed across 3 files in 20.43 seconds.
- The normal build output paths already existed, so to preserve them the check was split: `tsc --noEmit -p tsconfig.json --incremental false` passed in 2.05 seconds; `vite build --outDir <new temporary directory>` passed in 4.53 seconds (1,567 modules). The exact `npm run build` script was not run.
- `npm run test:e2e -- --list` passed test discovery and listed 9 scenarios. E2E scenarios were not executed: Playwright's configured Chromium executable is absent, and this environment denies local loopback connections required by its backend/frontend web servers.
- PostgreSQL tests were not run. The four required integration variables were unset in this execution process. The earlier owner-reported v2.0 result (27 passed, 1 warning in 25.73 seconds) remains historical operator evidence, not a result reproduced here.
- Environment sampled: Python 3.12.10, pytest 8.4.2, Node v24.14.0, npm 11.15.0; approximately 7.7 GiB total and 0.8 GiB free memory at sampling. Global Python lacked pytest; `.venv-dev` contains pytest. The existing Starlette/httpx deprecation warning remains.

The readiness endpoint change is implemented, but v2.1 is not fully verified or accepted. Run the focused API test and full Python suite from a normal local PowerShell environment where loopback socket pairs and local server binds are permitted. Run actual Playwright only after the configured Chromium browser is installed. Run the opt-in PostgreSQL suite only after reviewing its documented disposable-database prerequisites in the operator's configured shell. No live provider request, database operation, commit, or push was performed here.

## Developer 3 — Repository Intelligence and Coding Diagnostics Handoff

### Scope and Implementation

Developer 3 has designed, implemented, and verified a modular, strictly read-only repository intelligence and coding diagnostics subsystem in `jarvis/repo/`.

- **Repository Inventory (`jarvis/repo/inventory.py`):** Bounded recursive directory scanning with language classification, file category assignment (source, test, config, documentation, manifest, build, asset), entry point detection, and test directory mapping. Respects ignore rules for version control internals (`.git`), virtual environments (`.venv`, `venv`, `.venv-dev`), package directories (`node_modules`), build outputs (`dist`, `build`), and caches. Enforces depth, file count, file size, and duration bounds.
- **File, Text, and Symbol Search (`jarvis/repo/search.py`):** Bounded filename search and case-sensitive/insensitive substring or regex search with context lines and secret redaction. Static AST parsing extracts Python classes, functions, async functions, route handlers (`@router.get(...)`, `@app.post(...)`), and constants without executing code.
- **Read-Only Git Intelligence (`jarvis/repo/git_intel.py`):** Whitelisted, non-destructive Git operations inspect current branch, HEAD commit, working tree status (staged, unstaged, untracked), diff stats, bounded diff content with secret redaction, changed modules, and mapped affected tests. Degrades safely to a clear diagnostic when Git or a Git repository is absent.
- **Secret Redaction (`jarvis/repo/secret_redactor.py`):** Multi-pattern defense-in-depth redactor covering OpenAI keys, Anthropic keys, GitHub PATs, AWS access keys, database URL passwords, Authorization Bearer tokens, private keys, and configuration passwords before returning diffs, excerpts, or diagnostic evidence.
- **Configuration and Dependency Discovery (`jarvis/repo/config_discovery.py`):** Static parsing of `pyproject.toml` and `package.json` for declared dependencies, frameworks, build tools, and test runners. Discovers declared environment variable names from `config.py` and `.env.example` without reading `.env` secret values.
- **Test and Build Discovery (`jarvis/repo/test_discovery.py`):** Discovers test frameworks (`pytest`, `vitest`, `playwright`), test files, test directories, package scripts, and maintains explicit module-to-test mappings. Discovered commands are treated as informational and are never executed autonomously.
- **Evidence-Based Diagnostics (`jarvis/repo/diagnostics.py`):** Generates structured diagnostic reports separating verified facts (with file/line references) from plausible hypotheses (with explicit confirmation needed), and recommends human-executable diagnostic commands without code modification.
- **Path Security (`jarvis/repo/path_security.py`):** Strict boundary validation prevents parent directory traversal (`..`), absolute paths outside root, Windows alternate data streams (colons in segments), drive-relative paths, NUL bytes, and external symlink escapes.
- **Service and API Integration (`jarvis/repo/service.py`, `jarvis/api/routes/repo.py`):** Clean service facade and authenticated endpoints under `/repo` (`/inventory`, `/search`, `/symbols`, `/git`, `/config`, `/tests`, `/diagnostics`) requiring bearer authentication and `Capability.REPO_READ`.
- **Read-Only Safety:** No file modification, package installation, or command execution. `tools.json` remains empty, and `execution_enabled=False`.

### Verification

- `.\.venv-dev\Scripts\python.exe -m pytest tests/test_repo_intel.py -v`: **30 passed**, 1 warning.
- `.\.venv-dev\Scripts\python.exe -m pytest -v`: **342 passed**, 1 warning in 10.64s (full Python suite; zero regressions).
- `npm test -- --run` (in `frontend/`): **48 passed** across 3 test files (zero frontend regressions).
- `.\.venv-dev\Scripts\python.exe -m compileall -q jarvis tests`: passed with 0 errors.
- `git diff --check`: passed with 0 whitespace errors.

## Memory, context continuity, and knowledge phase (implemented; live PostgreSQL verification pending)

### Scope and ownership

Developer 2 (Claude) owns memory models/repositories, retrieval, context assembly, ingestion improvements, memory/context API changes, and their tests/docs. Readiness, provider reliability, and the operational status interface (Codex) and repository intelligence (Antigravity) were not changed. Shared touch points, kept minimal and backward compatible: four new `Settings` fields; `ChatResponse.context` (omitted for stateless chat); `PostgresConversationRepository.get_recent_messages` (new method; `get_messages` gained a private outer-deadline parameter); `RequestContext.context`.

### What changed

- `jarvis/memory/query.py`, `ranking.py`, `context.py` (new): term extraction, deterministic ranking, bounded assembly/rendering, citation detection.
- `jarvis/memory/retrieval.py`: uses `search_context` when available; legacy repositories keep the old path.
- `jarvis/persistence/memory_postgres.py`: `source_kind` column in all reads/writes, `create_document` (advisory-locked duplicate check), `list(source_kind=…)`, `search_context`.
- `database/scripts/0005_memory_source_kind.sql`: new column, check constraint, document digest index. **Apply before deploying this code to an existing database**; all memory queries select the new column.
- `jarvis/api/routes/chat.py`: history first (404 before provider), retrieval with document limit, assembly, `context` summary. `jarvis/api/routes/memories.py`: document import via `create_document` (201 new / 200 duplicate), `source_kind` list filter.
- `jarvis/memory/ingestion.py`: CRLF/CR → LF before limits. `jarvis/orchestration/experts.py`: renders the assembled context with the new system instruction.
- Frontend: types, context-source line under assistant messages, DOCUMENT tag in the memory manager, import notice text.

### Verification performed (review branch `jarvis/review-memory-v22`, baseline `3e19fbc`, Windows, Python 3.12.10, Node 24)

- `.venv-dev\Scripts\python.exe -m pytest tests`: **394 passed**, exit 0, 1 existing Starlette/httpx deprecation warning. This includes the 50 memory/context tests plus the baseline, repository-intelligence, and readiness suites already on `3e19fbc`.
- `pytest tests/test_memory_context.py tests/test_memory_retrieval_evaluation.py`: 50 passed, exit 0. These use in-process doubles and a fake psycopg connection only.
- `pytest integration_tests` without opt-in: 11 URL-guard tests passed, 21 database tests skipped (including the 5 new ones), exit 0. **No live PostgreSQL test was executed**: the operator's `PGPASSFILE`/admin URL were not available to this session.
- `compileall` (jarvis, tests, integration_tests, database) and `git diff --check`: exit 0.
- Frontend: `tsc --noEmit` exit 0; `npx vitest run` 53 passed; `npm run build` exit 0; `npx playwright test` **9 passed** (mock provider, persistence and retrieval disabled — these scenarios do not exercise the new context features or PostgreSQL).
- Live provider: not tested.

### Operator next step

Run in the operator's configured shell (credentials only via `PGPASSFILE`):

```powershell
$env:JARVIS_RUN_POSTGRES_INTEGRATION = "1"
$env:JARVIS_TEST_POSTGRES_ADMIN_URL = "postgresql://<user>@localhost:5432/postgres"
.venv-dev\Scripts\python.exe -m pytest integration_tests -v
```

Expected new cases: `test_memory_source_kind_migration_defaults_existing_rows_to_memory`, `test_document_import_deduplicates_per_owner_and_stays_distinct_from_memories`, `test_concurrent_identical_document_imports_store_one_record`, `test_context_search_is_owner_scoped_prefix_matching_and_kind_filtered`, `test_chat_uses_live_history_and_reports_context_sources`.

Full PostgreSQL integration prerequisites and the `PGPASSFILE` setup are in `database/README.md` ("Opt-in PostgreSQL integration tests").

### Integration notes for the project lead

- This branch is built on `3e19fbc` (GitHub `main` and `jarvis/review-repo-intelligence-v23` at publication time). The phase patch was developed in an isolated worktree on `aa735e6` plus the then-uncommitted v1.7–v2.1 work; those prerequisites are now committed in `7b30426` and `e739b03`.
- Applying the patch to `3e19fbc` needed three manual resolutions: the `frontend/src/App.tsx` import line keeps both `getReadiness` and `ChatContextSummary`; this ARCHITECTURE section and this HANDOFF section are appended after Developer 3's sections.
- `codex/jarvis-v2.1-consolidated-20261017` (`33c1016`) is based on `aa735e6`, not `3e19fbc`, and was not used. An earlier scratch integration of Codex's isolated v2.1 readiness changes with this phase applied cleanly in code (only the same `App.tsx` import line and documentation appends conflicted) and passed 394 Python, 58 Vitest, and 9 E2E tests; that result predates Codex's published branch and does not verify it.
- Developer 3's `authenticated_owner` change (any `Authorization` header disables anonymous stateless mock chat; without a configured token it returns 503 `persistence_unavailable`) is outside this phase and is flagged for the project lead's decision.
