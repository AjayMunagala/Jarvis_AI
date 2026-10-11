# JARVIS Handoff for the Next Coding Assistant

## Current milestone

JARVIS Core v0.1 remains the offline FastAPI demo core. A separate Windows-oriented PostgreSQL provisioning foundation now exists under `database/`; it does not change backend startup or persist chat. The working tree may also contain uncommitted frontend QA remediation; preserve it.

## Implemented

- FastAPI application factory with startup/shutdown logging and validated configuration.
- `/health`, `/ready`, and `/chat` endpoints.
- Typed provider protocol and deterministic mock provider, clearly marked as demo output.
- Configurable request body byte ceiling, message character length, and provider timeout; consistent sanitized API error envelopes.
- JSON structured logging without chat text or credentials.
- Tests for health/readiness, demo chat, invalid requests, provider failure/timeouts, request/message limits, configuration validation, message log redaction/capture, root-handler preservation, unexpected errors, and provider selection.
- Verified baseline: `python -m pytest -v` passes 17 tests. The installed test client emits a Starlette deprecation warning recommending `httpx2`.
- Windows install/run/test instructions and architecture/roadmap docs.
- Standalone database scripts create the target only when absent, apply checksummed migrations transactionally, verify migration metadata, and create validated timestamped backups. The initial migration creates only `jarvis_meta.schema_migrations`.

## Project structure

See `jarvis/` for source, `tests/test_api.py` for tests, and `docs/` for architecture, roadmap, and this handoff.

## Key decisions

- Keep a modular monolith and provider protocol; defer application persistence, tools, and model SDKs. Database provisioning remains optional and outside FastAPI startup.
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
- No real LLM, chat persistence, auth, rate limiting, memory, tool execution, or external integrations exist. The React UI is local and keeps conversation state in memory only.
- PostgreSQL 18 CLI binaries were found on this Windows machine, but no active listener was detected on ports 5432–5439. A separate SCRAM-authenticated PostgreSQL 18 cluster on port 55439 validated install, repeat-migration skip, verification, backup archive inspection, and restore. The user's pgAdmin server settings and existing `jarvis_AI` database were not queried or modified; confirm their host, port, role, and credentials locally.
- Readiness currently indicates successful app construction, not dependency checks.
- Provider timeouts use `asyncio.wait_for`, which cancels the provider coroutine; future async adapters must handle cancellation and release any held resources.

## Exact next milestone

Before using the database scripts operationally, validate them against a disposable PostgreSQL test database and perform a restore drill. Application persistence is still deferred; do not connect the mock chat to PostgreSQL until a separate milestone defines its data model and retention behavior. The Core v0.2 orchestration and provider-seam work described in `ROADMAP.md` also remains separate from this database tooling.
