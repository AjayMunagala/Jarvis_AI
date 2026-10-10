# JARVIS Handoff for the Next Coding Assistant

## Current milestone

JARVIS Core v0.1: offline FastAPI demo core. This repository began empty; the starter was created without replacing existing user files. The project is now in Git on `main`; Python 3.12.10 is the verified Windows interpreter.

## Implemented

- FastAPI application factory with startup/shutdown logging and validated configuration.
- `/health`, `/ready`, and `/chat` endpoints.
- Typed provider protocol and deterministic mock provider, clearly marked as demo output.
- Configurable request body byte ceiling, message character length, and provider timeout; consistent sanitized API error envelopes.
- JSON structured logging without chat text or credentials.
- Tests for health/readiness, demo chat, invalid requests, provider failure/timeouts, request/message limits, configuration validation, message log redaction/capture, root-handler preservation, unexpected errors, and provider selection.
- Verified baseline: `python -m pytest -v` passes 17 tests. The installed test client emits a Starlette deprecation warning recommending `httpx2`.
- Windows install/run/test instructions and architecture/roadmap docs.

## Project structure

See `jarvis/` for source, `tests/test_api.py` for tests, and `docs/` for architecture, roadmap, and this handoff.

## Key decisions

- Keep a modular monolith and provider protocol; defer database, frontend, tools, and model SDKs.
- The `mock` provider is the only supported provider, intentionally requiring no network, key, or GPU.
- Provider errors are generic in HTTP responses; message contents are not logged.
- `Settings` validates `JARVIS_*` environment variables using Pydantic Settings. Message length has a 20,000-character hard schema ceiling; body bytes have a configurable 65,536-byte default and 1,024-to-1,048,576 range.
- Logging intentionally omits exception messages and tracebacks to avoid leaking sensitive provider details; it records exception types. Debugging unexpected failures is therefore less detailed.
- Uvicorn's base dependency supports reload through stat polling; `uvicorn[standard]` is optional for WatchFiles-based reload.

## Commands

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m uvicorn jarvis.main:app --reload
python -m pytest -v
```

## Known issues and limitations

- Dependencies were not preinstalled in the initial environment; install from `pyproject.toml` before running.
- No real LLM, persistence, auth, rate limiting, memory, tool execution, UI, or external integrations exist.
- Readiness currently indicates successful app construction, not dependency checks.
- Provider timeouts use `asyncio.wait_for`, which cancels the provider coroutine; future async adapters must handle cancellation and release any held resources.

## Exact next milestone

Implement **Core v0.2 — orchestration and provider seams** as described in `ROADMAP.md`: add an orchestration service and request context between routes and providers, provider contract conformance coverage including timeout/failure cases, and typed capability/tool registry interfaces without enabling external actions. Keep the mock path fully offline and preserve explicit boundaries for future permissions and audit events.
