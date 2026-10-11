# JARVIS Handoff for the Next Coding Assistant

## Current milestone

JARVIS Core v0.7 adds conversation identity, continuation, and a read-only paginated history API to the optional PostgreSQL persistence foundation. Persistence remains disabled by default, and mock mode does not need a PostgreSQL driver or server. New persistent chats create a conversation ID; a supplied existing ID continues that conversation atomically. Unknown IDs return 404, and history/continuation requests return 503 when persistence is disabled. The history route returns chronological pages of up to 100 messages. IDs are not authorization; accounts and ownership checks do not exist. Apply migrations using the existing Windows migration runner; the app does not migrate at startup.

## Implemented

- FastAPI `/health`, `/ready`, and `/chat` endpoints, validated configuration, request limits, sanitized errors, and structured logging.
- Vendor-independent provider protocol, deterministic offline mock provider, and optional OpenAI Chat Completions adapter with validated output, sanitized failures, explicit connect/read timeouts, and bounded transient retries through the orchestrator.
- Orchestration contracts for request context, intent, execution plan, expert output/result, audit events, and internal response.
- Safe deterministic intent classification with `general_inquiry`/confidence `0.0` fallback on classifier failure.
- Pydantic-validated expert and tool registry configuration under `jarvis/orchestration/config/`. The seven configured personas route to the existing shared provider; they are not separate reasoning agents.
- Bounded planner (maximum five rounds), sequential by default. Parallel execution requires an explicitly configured intent and uses a semaphore bounded by settings.
- Pydantic output validation, explicit-transient-only exponential retries, one shared request deadline, dependency-aware execution, and partial synthesis when successful outputs remain.
- Metadata-only logs/audit-event schemas; message text, provider output, exception text, and credentials are excluded. Audit events are in-memory only.
- The `/chat` response retains `reply`, `provider`, and `demo` and adds a `partial` boolean. Frontend renders a partial-result label when that value is true. Combined expert output is clipped at 40,000 characters with a visible truncation marker; clipping does not set `partial`.
- Chat success, provider failure, and timeout responses carry an `X-JARVIS-Execution-ID` header that correlates with safe structured log and audit metadata. `provider` and `demo` identify the selected response source without changing the response contract.
- AnalysisExpert and ValidationExpert have separate configured system instructions. Regression tests verify that each instruction reaches OpenAI request payloads through mocked HTTP. They still share one provider adapter; the mock does not perform model reasoning.
- AnalysisExpert and ValidationExpert use bounded Pydantic structured-output contracts. The OpenAI adapter requests strict Chat Completions JSON Schema after transforming a copy to remove string/array size bounds and documented unsupported composition keywords; local Pydantic validation retains all original length, collection, required-field, and semantic checks. The OpenAI guide marks `maxItems` as supported for standard models but not fine-tuned models, so all array bounds are omitted for model-selection compatibility. The adapter validates strict structure, object closure, required properties, and local references before sending, and fails sanitized if any feature remains unsupported. Mocked requests verify schema payload construction, not live endpoint compatibility. Truncated structured completions fail permanently. Structured calls are bounded to 8,192 completion tokens; ordinary text calls remain at 1,024. Mock results preserve the historical deterministic response and are marked `not_evaluated` with empty specialist fields. Shape validation does not establish semantic accuracy.
- OpenAI refusal responses and malformed, empty, or whitespace-only completion content are sanitized permanent provider failures; no retry or mock fallback occurs.
- Tool configuration is currently empty, the registry has no executor, and no shell command or external/state-changing action can be invoked.
- Standalone Windows PostgreSQL provisioning scripts remain optional and outside application startup.
- Optional `jarvis.persistence` provides a small repository protocol and PostgreSQL adapter. The `persistence` extra declares `psycopg[binary]`; set `JARVIS_PERSISTENCE_ENABLED=true` and a secret `JARVIS_PERSISTENCE_DATABASE_URL` only in a local environment. `/chat` returns sanitized HTTP 503 if saving fails. The same response shape is retained on success.
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
- The only configured tool list is empty. Tool execution, permission/approval gateway, persistent memory, persistent audit storage, authentication, and external integrations are not implemented. Conversation persistence is opt-in; v0.7 can link request/response exchanges by conversation ID and read bounded history pages, but has no user ownership or frontend history UI.
- `/ready` confirms successful settings/provider/orchestrator construction; it does not probe external systems.
- The default provider runs without a GPU, API key, or internet. Frontend conversation state is in-memory only.
- OpenAI selection requires `JARVIS_OPENAI_API_KEY`; setting it does not fall back to mock if calls fail. Requests send user message content to the configured endpoint and may incur charges. No live provider call was performed during tests.
- Analysis and validation require an endpoint/model that supports strict Chat Completions JSON Schema output. Unsupported-schema behavior is verified using mocked HTTP responses; live compatibility, model quality, and network connectivity remain unverified.
- Provider cancellation is performed by `asyncio.wait_for`; future asynchronous adapters must release resources when cancelled.
- The PostgreSQL foundation and its restore drill are documented in `database/README.md`; operational use still requires confirming the user's local server configuration and restore target.
- Persistence can link multiple exchanges with a client-supplied conversation ID and expose bounded history pages. Multi-user ownership, authentication, frontend history UI, and a PostgreSQL integration test against a live server are not included unless verification below says otherwise.

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

## Next work

Review the Core v0.7 changes before approving another milestone. Live PostgreSQL migration/adapter behavior, live OpenAI connectivity, strict-schema compatibility against a live endpoint, and semantic model quality remain unverified. Preserve the roadmap's longer-term goals and do not infer that future tools, memory, model-driven planning, or actions exist from current configuration interfaces.
