# JARVIS Handoff for the Next Coding Assistant

## Current milestone

JARVIS Core v0.3 adds an optional OpenAI Chat Completions adapter behind the existing provider interface. Mock remains the default and runs offline. PostgreSQL setup remains standalone. The live OpenAI endpoint has not been verified.

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
- The validation expert has one configured system instruction for real providers; all experts still share a provider adapter, and the mock does not perform model reasoning.
- Tool configuration is currently empty, the registry has no executor, and no shell command or external/state-changing action can be invoked.
- Standalone Windows PostgreSQL provisioning scripts remain optional and outside application startup.

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

- Intent classification and planning are deterministic, not model-driven. The configured personas share one provider; only the validation expert has a distinct model instruction. In mock mode that instruction is ignored and responses are deterministic demo text, not model reasoning.
- The only configured tool list is empty. Tool execution, permission/approval gateway, persistent memory, persistent audit storage, application database integration, authentication, and external integrations are not implemented.
- `/ready` confirms successful settings/provider/orchestrator construction; it does not probe external systems.
- The default provider runs without a GPU, API key, or internet. Frontend conversation state is in-memory only.
- OpenAI selection requires `JARVIS_OPENAI_API_KEY`; setting it does not fall back to mock if calls fail. Requests send user message content to the configured endpoint and may incur charges. No live provider call was performed during tests.
- Provider cancellation is performed by `asyncio.wait_for`; future asynchronous adapters must release resources when cancelled.
- The PostgreSQL foundation and its restore drill are documented in `database/README.md`; operational use still requires confirming the user's local server configuration and restore target.

## Verification on this working tree

- `.venv\Scripts\python.exe -m pytest -v`: 74 passed; one Starlette test-client deprecation warning recommending `httpx2`.
- `npm test -- --run` from `frontend/`: 16 passed.
- `npm run build` from `frontend/`: passed.
- `git diff --check`: passed.
- OpenAI adapter request/response, timeout, retry classification, sanitization, configuration, and API metadata tests use mocked HTTP transport. No live provider call was made.

## Next work

Review Core v0.3 results and approve the next milestone before implementing it. Preserve the roadmap's longer-term goals and do not infer that future tools, memory, model-driven planning, or actions exist from the current configuration interfaces.
