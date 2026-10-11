# JARVIS Handoff for the Next Coding Assistant

## Current milestone

JARVIS Core v0.4 extends the optional OpenAI Chat Completions adapter with distinct configured instructions for AnalysisExpert and ValidationExpert and explicit rejection of refusal/malformed/blank completions. Mock remains the default and runs offline while ignoring specialist instructions. PostgreSQL setup remains standalone. The live OpenAI endpoint has not been verified.

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
- Provider replies are validated as nonblank bounded advisory text. A persona-specific structured JSON output contract and semantic specialist-quality checks are not implemented.
- OpenAI refusal responses and malformed, empty, or whitespace-only completion content are sanitized permanent provider failures; no retry or mock fallback occurs.
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

- Intent classification and planning are deterministic, not model-driven. The configured personas share one provider; AnalysisExpert and ValidationExpert have distinct model instructions. In mock mode both instructions are ignored and responses are deterministic demo text, not model reasoning.
- The only configured tool list is empty. Tool execution, permission/approval gateway, persistent memory, persistent audit storage, application database integration, authentication, and external integrations are not implemented.
- `/ready` confirms successful settings/provider/orchestrator construction; it does not probe external systems.
- The default provider runs without a GPU, API key, or internet. Frontend conversation state is in-memory only.
- OpenAI selection requires `JARVIS_OPENAI_API_KEY`; setting it does not fall back to mock if calls fail. Requests send user message content to the configured endpoint and may incur charges. No live provider call was performed during tests.
- Provider cancellation is performed by `asyncio.wait_for`; future asynchronous adapters must release resources when cancelled.
- The PostgreSQL foundation and its restore drill are documented in `database/README.md`; operational use still requires confirming the user's local server configuration and restore target.

## Verification on this working tree

- `.venv\Scripts\python.exe -m pytest -v`: 83 passed in 1.28s when run with sandbox restrictions lifted.
- `.venv\Scripts\python.exe -m pytest tests/test_intelligence_provider.py -v`: 26 passed in 0.65s when run with sandbox restrictions lifted.
- The earlier sandbox hang was traced with a bounded faulthandler dump to Windows asyncio Proactor and Selector loop startup blocking in Python's `socket.py::_fallback_socketpair`, at local `accept()`. A local `socket.socketpair()` and `asyncio.run(asyncio.sleep(0))` work with sandbox restrictions lifted. This was a sandbox loopback restriction, not a JARVIS production-code defect. Run Python tests in an environment that permits loopback socket creation.
- Python is 3.12.10 and the active pytest is 9.1.1 although `pyproject.toml` declares pytest `<9.0`. The TestClient warning recommends `httpx2`; it is not installed or declared. Since the stack trace showed the loopback restriction as the cause and the suite passes with these installed versions outside the sandbox, dependencies were left unchanged. `pip check` reported no broken requirements.
- `npm test -- --run` from `frontend/`: 16 passed.
- `npm run build` from `frontend/`: passed.
- `.venv\Scripts\python.exe -m compileall -q jarvis`: passed.
- `git diff --check`: passed.
- OpenAI request/refusal/error tests use mocked HTTP transport; no live provider call was made.

## Next work

Review the Core v0.4 changes before approving another milestone. Live OpenAI connectivity and the semantic quality of live model reasoning remain unverified. Preserve the roadmap's longer-term goals and do not infer that future tools, memory, model-driven planning, or actions exist from the current configuration interfaces.
