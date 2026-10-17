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

## Core v1.6 — Bounded PostgreSQL persistence operations (implemented and verified)

- Add a dedicated `JARVIS_PERSISTENCE_OPERATION_TIMEOUT_SECONDS` budget (default 5 seconds, range 1–60) across synchronous conversation and memory repository operations.
- Check the budget before connecting and before each SQL statement; use parameterized transaction-local PostgreSQL `statement_timeout` values based on remaining operation time. Preserve atomic exchange writes and rollback/recovery behavior.
- Keep the v1.5 shared deadline for retrieval and expert execution separate. Memory search honors the earlier of the two deadlines; post-synthesis conversation persistence retains existing sanitized HTTP 503 behavior under its own budget.
- Preserve mock/offline mode, authentication, owner predicates, API schemas, and privacy-safe error/log behavior. No migration or dependency is introduced.

Verification results supplied by the repository operator (not rerun during the documentation and commit task): 219 Python tests passed; all 18 opt-in PostgreSQL integration tests passed in 18.47 seconds, including the conversation statement-timeout rollback/recovery case against the configured live test environment; 30 frontend tests passed; frontend production build, Python compilation, and `git diff --check` passed. The Python and PostgreSQL suites each reported one existing Starlette/httpx TestClient deprecation warning. Gemini's APPROVE WITH CONDITIONS audit required live PostgreSQL verification, which was subsequently completed. No live OpenAI request was made.

## Core v1.7 Stage 2 — Evaluation-first retrieval quality (evaluation complete; ranking unchanged)

- Extend the opt-in PostgreSQL lexical retrieval evaluation with three deterministic, owner-scoped synthetic length-sensitivity queries. Compare current `ts_rank_cd` normalization 0 with candidate normalization 1 while keeping matching, owner filtering, limit, tie-breakers, and corpus relevance labels fixed. Preserve the 12 Stage 1 expected rankings as regression checks.
- Measured across the three synthetic queries: current mean Precision@5 0.2, Recall@5 1.0, and MRR@5 0.3333; candidate mean Precision@5 0.2, Recall@5 1.0, and MRR@5 1.0. The mean MRR@5 gain was 0.6667.
- The candidate regressed the Stage 1 `common` query's expected top-five order. It is therefore not adopted, despite clearing the length-sensitive MRR threshold. Production ranking SQL remains unchanged.
- These controlled synthetic results demonstrate behavior only for the defined corpus and queries; they do not establish general retrieval quality or justify semantic search. No schema, migration, dependency, API, frontend, or production ranking change is included.

## Core v1.7 — Bounded document ingestion (implemented; latest live result operator-reported)

- Add an explicit, authenticated single-file import for `.txt` and `.md`, creating one existing owner-scoped memory record per upload with a sanitized basename source reference.
- Bound raw input to 16 KiB and decoded UTF-8 content to 4,000 characters; strip one leading BOM and reject malformed encoding, NUL, blank text, and unsupported extensions before persistence.
- Require the user to confirm the visible filename before browser upload. Store/display content as plain text; do not interpret Markdown, follow links, or execute embedded material.
- Preserve retrieval opt-in/default-off behavior, five-record maximum, existing lexical ranking, deadlines, fallback and untrusted-context framing. No migration, chunking, embeddings, semantic search, or automatic extraction is part of this increment.
- Earlier implementation verification (operator-reported): 245 Python tests passed, compileall passed, 33 frontend tests passed, and the frontend build passed. This is an earlier snapshot, not the latest offline result.
- Earlier live PostgreSQL verification (operator-reported): 20 passed, 0 failed, with cleanup-verification code present. That run predates the later filename-validation and documentation fixes and does not verify the latest working tree. It covers successful post-drop absence verification, not every possible cleanup failure scenario.
- Later verification after the filename-validation and documentation fixes (operator-reported): the full Python suite passed 250 tests; compileall and `git diff --check` passed. PostgreSQL integration tests were not rerun after those fixes. These results were not independently reproduced during the current documentation update.
- Follow-up verification run during the Windows drive-relative filename fix: 21 ingestion unit tests, 12 focused API tests (31 deselected), and the full Python suite (252 passed, one existing Starlette/httpx deprecation warning) passed. Compileall and `git diff --check` passed. PostgreSQL integration tests were not rerun after this latest filename fix; frontend tests/build were not rerun because frontend files were unchanged.

## Core v0.9 — Persistent Memory and Retrieval Foundation (implemented; verified locally)

- Add an incremental PostgreSQL memory migration and a separate memory repository protocol/adapter. Memory content is strictly bounded, explicitly created/updated/deleted, and never copied automatically from chat messages.
- Add bounded `/memories` create/list/get/patch/delete and deterministic PostgreSQL lexical full-text search. Search is not semantic and does not use embeddings or pgvector.
- Make chat retrieval explicitly configurable and disabled by default. Pass a small typed set of retrieved records as untrusted context; retrieval failures fall back to no memory context.
- Preserve offline mock startup and ordinary chat when persistence is disabled. Memory API operations return sanitized 503 when storage is unavailable.
- Document that the memory API is global and unauthenticated and must not be exposed to untrusted/shared deployments; UUIDs do not grant ownership or authorization.

Verification on Windows/Python 3.12.10: 143 default Python tests and 13 focused memory tests passed; the opt-in PostgreSQL integration suite passed 12/12 (5 URL checks and 7 live PostgreSQL cases); compileall, 16 frontend tests, the production build, and `git diff --check` passed. Live OpenAI behavior and semantic quality remain unverified. The milestone adds no account/ownership model, embedding generation, pgvector, automatic memory extraction, retention scheduler, memory audit history, or authentication/ownership controls.

## Core v1.8 — Centralized authorization policy foundation (implemented)

- Add a server-side allowlist for advisory chat, memory read/write, and conversation read/write, applied after the existing single-owner bearer authentication. Analysis and recommendation classifier labels are not distinct security grants.
- Preserve unauthenticated stateless mock chat only while persistence is disabled. Missing owner/policy decisions and unknown capabilities are denied; external action execution is never granted.
- Keep memory owner predicates, the empty tool registry, and `execution_enabled=False`. Prompts, model output, plans, and tool metadata cannot authorize operations.
- Approval records/API/UI, `workflow.preview`, migrations, multi-user roles, and external-action execution remain deferred because no typed workflow operation currently exists to approve.

## Core v1.9 — Integration, security, and workflow verification (implemented; independent report recommends acceptance)

- Require the existing configured bearer identity for `/chat` whenever the trusted server-side provider is non-demo, even when persistence is disabled. Preserve anonymous stateless mock chat and all persistence-backed authentication and owner checks.
- Add route-level denial regression tests for every memory read/write API operation and a deterministic API workflow covering authenticated document ingestion, owner-scoped lexical search, memory context passed into orchestration/provider execution, and chronological conversation history.
- Extend the opt-in PostgreSQL fixture with read-only reporting for pre-existing generated test databases. Active connections are reported as active; connection-free entries are reported as stale-uncertain. The fixture never automatically deletes pre-existing databases because the available metadata cannot prove staleness or exclude an active process between connections.
- Preserve advisory-only behavior, the empty tool registry, `execution_enabled=False`, retrieval defaults/limits, and current APIs. A supplied independent report recommends acceptance; live-provider behavior remains unverified.

## Core v2.0 — Integrated local platform (Phases 1–6 implemented; final acceptance pending)

- **Phase 1 — Conversation summary contract and repository:** independently reviewed. Reuses the existing schema and persistence model; no migration was added.
- **Phase 2 — Authenticated conversation-list API:** independently reviewed. `GET /conversations` requires the existing bearer identity and `conversation.read`; limit defaults to 20 (1–100), offset defaults to 0 (0–1,000,000), and the repository supplies `has_more`. Ordering is `last_activity_at DESC, conversation_id DESC`; previews are nullable and at most 120 Unicode characters, sourced from the first user message.
- **Phase 3 — PostgreSQL integration verification:** test implementation and safety review are recorded. Latest live result is owner-reported: the project owner manually ran the suite in PowerShell and reported **27 passed, 1 warning in 25.73 seconds**. The warning was a `StarletteDeprecationWarning` concerning `httpx` and `starlette.testclient`, not a test failure. The final read-only Codex review did not rerun the tests; this is owner-provided execution evidence, not an independently reproduced result from that review. The supplied result does not independently establish post-run database absence. Do not treat a code review or browser interception as live database verification.
- **Phase 4 — Frontend conversation integration:** independent audit reported PASS. The client lists/reopens saved conversations, navigates bounded pages, loads history for display, and clears credentials and sensitive in-memory state on lock or HTTP 401. It does not inject history into provider prompts.
- **Phase 5 — Playwright browser smoke tests:** independent audit reported PASS WITH FINDINGS; the recorded suite has nine scenarios. It uses local loopback services and the mock provider. Synthetic intercepted conversation API responses test UI behavior only and do not prove persisted database behavior.
- **Phase 6 — Documentation and final regression:** this handoff records the current implementation and verification. Final v2.0 acceptance remains pending project-owner review and the stated cleanup-evidence limitation.

Current closure-run results are recorded in `docs/HANDOFF.md`. The policy remains single-owner and advisory-only; `tools.json` is empty and `execution_enabled=False`. No approval workflow, external-action executor, multi-user authorization, or production-readiness claim is included.

## Core v2.1 — Reliability and operational readiness (owner accepted with documented non-blocking findings)

- Improve operational diagnostics without changing the modular-monolith architecture or exposing sensitive configuration.
- Keep `/health` as process liveness. Make `/ready` report fixed readiness states for application, provider, orchestration, authorization-policy, and persistence wiring; fail with sanitized HTTP 503 when a required component is unavailable.
- Treat intentionally disabled persistence as a valid ready mode. Report configured persistence only as repository wiring; do not connect to PostgreSQL from readiness.
- Preserve authentication, capability enforcement, mock mode, provider selection, owner isolation, bounded execution, and disabled tool/action execution.
- Add focused pure unit coverage for ready, incomplete, and persistence-disabled/configured states, plus HTTP contract regression coverage.
- The project owner accepted the integrated v2.1–v2.3 working tree with documented non-blocking findings. Independently executed results and separate owner-run PostgreSQL/native Windows evidence are recorded in the authoritative `docs/HANDOFF.md`; no commit, push, merge, deployment, or next phase is authorized by acceptance.

## Core v2.3 — Repository Intelligence and Coding Diagnostics (owner accepted with documented non-blocking findings)

- Add a secure, bounded, read-only repository intelligence subsystem (`jarvis/repo/`) for Project JARVIS.
- Implement bounded repository inventory (`jarvis/repo/inventory.py`): structure, language detection, manifest recognition, entry points, and test directories with strict ignore rules.
- Implement bounded text, regular expression, and AST symbol search (`jarvis/repo/search.py`): classes, functions, async functions, route handlers, and constants with line numbers and context lines.
- Implement strictly read-only Git intelligence (`jarvis/repo/git_intel.py`): branch, HEAD commit, working-tree status (modified, staged, unstaged, untracked), diff summaries, changed modules, and affected test mappings.
- Implement multi-pattern secret redaction (`jarvis/repo/secret_redactor.py`): redacts API keys (OpenAI, Anthropic), GitHub tokens, AWS keys, database URL passwords, Bearer tokens, and configuration passwords.
- Implement static configuration and dependency discovery (`jarvis/repo/config_discovery.py`): parses `pyproject.toml`, `package.json`, and discovers declared environment variable names without exposing `.env` secret values.
- Implement test and build discovery (`jarvis/repo/test_discovery.py`): test frameworks (`pytest`, `vitest`, `playwright`), test files, test commands, and module-to-test mappings.
- Implement evidence-based diagnostic report generation (`jarvis/repo/diagnostics.py`): separates confirmed facts from hypotheses with explicit confirmation needed and human-executable suggested commands.
- Implement strict path traversal and boundary security (`jarvis/repo/path_security.py`): rejects directory traversal, absolute path escapes, Windows alternate data streams, drive-relative paths, NUL bytes, and symbolic-link components when disallowed; resolved-path containment is rechecked. A concurrent filesystem change can still race the precheck.
- Expose authenticated, read-only API endpoints under `/repo` (`/inventory`, `/search`, `/symbols`, `/git`, `/config`, `/tests`, `/diagnostics`) requiring bearer authentication and `Capability.REPO_READ`.
- The subsystem is strictly read-only, does not execute arbitrary code, does not modify files, does not change Git state, and keeps `execution_enabled=False`.

## v2.1–v2.3 integration acceptance pass (owner accepted with documented non-blocking findings)

- Integration branch: `jarvis/integration-acceptance-v24`, based on `origin/main` at `3e19fbc65a8aed8612d944c79a913596803ce1fe`. v2.1 readiness and v2.3 repository intelligence were already on that base; v2.2 memory/context work was integrated from `origin/jarvis/review-memory-v22` at `e7840b906e04442e06b25b95694526a904bf32b9`. The obsolete Codex v2.1 branch was not cherry-picked because current main already contained newer implementation.
- The repository-path fixes now cover original-path validation in inventory, manifest discovery, search, and service/API callers, retaining resolved containment. The precheck narrows but does not eliminate filesystem races. Native cases skipped in the Codex execution environment; the separate owner-run repository/security evidence below closes that gate.
- Historical independently executed integration snapshot: full offline Python suite **396 passed, 2 skipped**; repo-intelligence tests **33 passed, 2 skipped**; frontend unit tests **53 passed**; production build and compileall passed; Playwright **9 passed**; PostgreSQL URL/collision guards **11 passed**. After remediation, the independently executed full Python result was **431 passed, 16 skipped, 1 warning**; frontend tests **56 passed**, build/compilation passed, and Playwright **9 passed**. These runs remain separate from owner-run results. The existing Starlette/httpx warning and unresolved pytest temporary reparse-point issue remain documented in `docs/HANDOFF.md`.
- **OWNER-RUN RESULTS REPORTED BY THE PROJECT OWNER:** live PostgreSQL integration **6 passed, 1 warning** against PostgreSQL 18.6; repository/security tests **84 passed, zero skipped, 1 warning**, including native caller tests. Codex reviewed the supplied output and did not independently execute these owner runs or rerun them during this documentation update. Browser mock/intercepted results do not establish PostgreSQL persistence.
- The project owner accepted v2.1–v2.3 with the retained limitations: single-owner conversation boundary, filesystem races, heuristic redaction, unverified live-provider behavior and worst-case resource use, and the unresolved old pytest temporary object. No next-phase implementation, commit, push, or merge is authorized.

## Core v2.2 — Memory, context continuity, and knowledge (owner accepted with documented non-blocking findings)

- Separate explicit memories from ingested documents with `source_kind` (migration 0005, no backfill); owner-scoped exact-duplicate document imports return the existing record (HTTP 200).
- Replace chat retrieval's all-words `plainto_tsquery` match with stopword-filtered, suffix-trimmed OR-prefix candidate matching in PostgreSQL plus deterministic Python ranking (coverage, phrase, density, recency, UUID), weak-match filtering, de-duplication, and per-kind limits. The explicit `/memories/search` API keeps all-words semantics.
- Add a pure, bounded context assembler: recent same-conversation turns, memories, and document excerpts with character budgets, deterministic ordering, `H/M/D` refs, and escaped-JSON untrusted framing. Unknown conversations are rejected before any provider call; history requires `conversation.read`; storage failures degrade with an explicit status.
- Report supplied sources (metadata only) in `ChatResponse.context`, including `cited_in_reply` and a count of invented refs; show them in the chat UI and mark documents in the memory manager.
- Controlled synthetic evaluation (12 chat-style queries, 16 records): legacy all-words rule hit@3 0.0833; new rule hit@3 1.0, recall@3 1.0, MRR@3 1.0, precision@3 0.7917. This describes only that corpus; it is not a general quality claim.
- Historical verification on review branch `jarvis/review-memory-v22` (baseline `3e19fbc`): 394 Python tests passed; 53 frontend tests, TypeScript, `npm run build`, and 9 Playwright E2E scenarios passed. The 5 new opt-in PostgreSQL tests were not executed in that historical run (no operator credentials available). Subsequent owner-run live evidence, including the added migration-upgrade test, is recorded separately in the integration acceptance section and `docs/HANDOFF.md`.

## Core v2.4 — Coding intelligence, repository understanding and proposed patches (owner accepted with documented limitations)

- Add authenticated `/coding/analyze` and a Coding workspace for bounded explain/locate/trace/diagnose/recommend/patch/review/test-discovery workflows, using existing repository safety and shared orchestration.
- Separate static facts, user-reported observations, unverified hypotheses, missing information, reviewable diffs and verification recommendations. Static/mock mode does not claim model reasoning or test success. Non-demo context disclosure requires explicit per-request consent; live-provider behavior remains unverified.
- Validate closed contracts, source snapshots, path/private exclusions, bounded exact replacements and new-file proposals. Diffs are generated only; no code application, test execution, shell commands, tool execution or autonomous action is introduced. No dependencies or database changes are required.
- Preserve the accepted v2.1–v2.3 changes and their separate test provenance. The owner accepted v2.4 on 2026-10-17. **OWNER-RUN RESULTS REPORTED BY THE PROJECT OWNER:** 17 native Windows tests passed, 0 failed, 0 skipped (14 mocked variants deselected; 1 existing warning; exit 0). Live-provider behavior remains unverified, PostgreSQL has not been rerun against v2.4, and single-owner/advisory-only security limitations remain. Evidence and local integration provenance are recorded in `docs/HANDOFF.md`. Local integration and a local commit are separately authorized; pushing, publishing, merging to main and v2.5 are not authorized.

## Core v2.5 — Verification, Quality Engineering and Evidence-Based Validation (implemented)

- Add a bounded software verification subsystem (`jarvis/verification/`) with an explicit 9-state lifecycle model: `not_checked`, `check_requested`, `check_running`, `check_passed`, `check_failed`, `check_timed_out`, `check_blocked`, `check_result_unavailable`, `inconclusive`.
- Implement in-memory static patch validation (`jarvis/verification/patch_verifier.py`): verifies preimage hashes against disk, checks target creation collision avoidance, and performs simulated postimage AST syntax validation (Python AST / JSON) without modifying files (`repository_modified = False`).
- Implement targeted verification planning (`jarvis/verification/planner.py`): maps touched paths to syntax checks and affected test suites using repository intelligence.
- Implement an isolated, sandboxed process runner (`jarvis/verification/runner.py`) with whitelisted profiles (`pytest_targeted`, `compileall`, `vitest_targeted`, `tsc_build`, `playwright_targeted`), strict environment sanitization, timeouts, process group cleanup, output truncation (32KB), and secret redaction.
- Enforce a mandatory Dual-Gate security policy: verification execution is disabled by default (`verification_execution_enabled = False`); runner commands are strictly blocked unless both server configuration and authenticated `Capability.VERIFY_EXECUTE` are granted.
- Expose authenticated API routes `/verification/plan`, `/verification/verify-patch`, `/verification/run`, and `/verification/profiles`.
- Integrate one-click patch verification into the React Coding workspace (`CodingPanel.tsx`) with accessible report cards, fact lists, and distinct visual badges for all 9 states.
- Verification evidence: full Python test suite passed (537 passed, 17 skipped, 0 failed), frontend Vitest suite passed (77 passed, 0 failed), Playwright E2E passed (10 passed, 0 failed), `compileall` passed with 0 errors, and frontend production build succeeded.

## Core v2.6 — Browser Intelligence & Web Automation (implemented; owner acceptance pending)

- Add `jarvis/browser/`: replaceable providers (deterministic mock, `httpx` HTTP provider without JavaScript, optional lazily imported Playwright adapter), bounded owner-scoped sessions, standard-library DOM extraction with server-assigned element ids, deterministic action classification, and an audit trail.
- Enforce URL policy (http/https only, no credentials, public addresses only, redirect re-checks; Playwright subresource interception), element/observation staleness checks, sensitive-field protection, limits, loop detection, and redaction. Consequential actions are never executed: they are recorded as `approval_required`.
- Add `/browser` routes with new `browser.read` / `browser.interact` capabilities, evidence-first research over supplied URLs, and browser checks using the accepted v2.5 `VerificationReport` contracts (execution gated by `JARVIS_BROWSER_CHECKS_ENABLED`, default false). Add a frontend Browser workspace.
- Follow-up: optional `browser` extra (`playwright==1.64.0`, owner-authorized) verified against real headless Chromium; HTTP-provider DNS-rebinding protection enforces the policy at connection time. Details: `docs/BROWSER_INTELLIGENCE.md`.

## Core v2.7 — Capability registry and read-only tool runtime (implemented; owner acceptance pending)

- Master Phase 4. Typed capability/tool/result/audit contracts, risk classes, strict input validation, server-side permission enforcement, timeouts and output limits, and audit events for every call.
- Registry populated with 8 read-only tools wrapping existing services (calculator, memory search, repository search and symbols, git status and diff summary, verification plan, browser observe); `JARVIS_TOOLS_ENABLED=false` by default; the orchestrator does not call tools yet (v2.8).
- `GET /capabilities`, `POST /tools/calls`, `GET /tools/audit` (in-memory, non-durable) and a Capabilities frontend view. pgvector remains deferred (D6). Details: `docs/TOOL_RUNTIME.md`.

## Core v2.8 — First complete action loop (implemented; owner acceptance pending)

- Master Phase 6. Chat requests can use registered read-only tools: rule-based (offline) or model-proposed calls, validated by trusted code, executed through the v2.7 runtime within the shared deadline and round/call limits, answered with untrusted tool evidence, and independently verified (citations and computed values).
- `ChatResponse.tool_activity` and a chat UI line show calls, statuses, and verification. Off by default (`JARVIS_CHAT_TOOLS_ENABLED=false`). Details: `docs/ACTION_LOOP.md`.

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
