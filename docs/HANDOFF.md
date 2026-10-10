# JARVIS Handoff for the Next Coding Assistant

## Current milestone

JARVIS Core v0.1: offline FastAPI demo core. This repository began empty; the starter was created without replacing existing user files. Python 3.12.10 was available at implementation time. The workspace was not a Git repository.

## Implemented

- FastAPI application factory with startup/shutdown logging and validated configuration.
- `/health`, `/ready`, and `/chat` endpoints.
- Typed provider protocol and deterministic mock provider, clearly marked as demo output.
- Configurable request body byte ceiling, message character length, and provider timeout; consistent sanitized API error envelopes.
- JSON structured logging without chat text or credentials.
- Tests for health/readiness, demo chat, invalid requests, provider failure, limits, and configuration validation.
- Windows install/run/test instructions and architecture/roadmap docs.

## Project structure

See `jarvis/` for source, `tests/test_api.py` for tests, and `docs/` for architecture, roadmap, and this handoff.

## Key decisions

- Keep a modular monolith and provider protocol; defer database, frontend, tools, and model SDKs.
- The `mock` provider is the only supported provider, intentionally requiring no network, key, or GPU.
- Provider errors are generic in HTTP responses; message contents are not logged.
- `Settings` validates `JARVIS_*` environment variables using Pydantic Settings. Message length has a 20,000-character hard schema ceiling; body bytes have a configurable 65,536-byte default and 1,024-to-1,048,576 range.

## Commands

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m uvicorn jarvis.main:app --reload
python -m pytest
```

## Known issues and limitations

- Dependencies were not preinstalled in the initial environment; install from `pyproject.toml` before running.
- No real LLM, persistence, auth, rate limiting, memory, tool execution, UI, or external integrations exist.
- No Git metadata was present when the project was inspected.
- Readiness currently indicates successful app construction, not dependency checks.

## Exact next milestone

Implement **Core v0.2 — orchestration and provider seams** as described in `ROADMAP.md`: add an orchestration service and request context between routes and providers, provider contract conformance coverage including timeout/failure cases, and typed capability/tool registry interfaces without enabling external actions. Keep the mock path fully offline and preserve explicit boundaries for future permissions and audit events.
