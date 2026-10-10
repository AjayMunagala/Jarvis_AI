# JARVIS Core v0.1

JARVIS is a modular foundation for a personal AI operating system. This first milestone is a small FastAPI service with a provider-independent interface and a clearly labeled offline demo provider.

## What works now

- `GET /health` and `GET /ready` report service status and version.
- `POST /chat` accepts a validated message and returns a structured response.
- The default `mock` provider works without an API key, internet access, GPU, or paid service. It echoes the received message with a clear demo label; it does not reason like an LLM.
- Provider timeouts, provider failures, invalid requests, and configured message limits are handled explicitly.
- Settings are validated at startup and JSON logs avoid request contents and secrets.
- API errors use a JSON `{ "error": ..., "detail": ... }` envelope. Validation errors return 422, oversized requests return 413, provider failures return 502, provider timeouts return 504, and unexpected application errors return a generic 500.

## Not implemented yet

There is no real LLM adapter, database, memory, planner, tool execution, approval system, frontend, account system, or external integration. The API never runs shell commands or external actions.

## Prerequisites

- Windows 10/11 and Python 3.11 or newer (developed with Python 3.12.10).
- PowerShell.
- No GPU or API credentials are needed for demo mode.

## Install on Windows (PowerShell)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

If PowerShell blocks virtual environment activation, use `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that PowerShell session and activate again.

## Run the application

```powershell
python -m uvicorn jarvis.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the API explorer. Check health at `http://127.0.0.1:8000/health` and readiness at `http://127.0.0.1:8000/ready`. Example chat request:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/chat -ContentType "application/json" -Body '{"message":"Hello JARVIS"}'
```

## Run the tests

```powershell
python -m pytest
```

## Select demo mode

Demo mode is the default. Set `JARVIS_PROVIDER=mock` in `.env` to make it explicit. Supported provider choices are validated; unsupported choices fail startup. `.env` is ignored by Git.

## Where future capabilities belong

HTTP contracts live in `jarvis/api/routes/`; request and response types live in `jarvis/models/`; validated settings and shared infrastructure live in `jarvis/core/`; model adapters and provider selection live in `jarvis/intelligence/`. Future capability modules should depend on a small provider/tool interface and be registered at the orchestration boundary, rather than importing a specific model SDK. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Known limitations and troubleshooting

- The mock response is deterministic text, not model output.
- Only the mock provider is implemented. Other provider values are rejected.
- In-memory state is not persistent; PostgreSQL and memory are later milestones.
- `/ready` confirms that settings and the configured provider were constructed; it does not probe external dependencies. Setting `JARVIS_ENVIRONMENT=production` only changes the validated environment label and does not add authentication, rate limiting, or production hardening.
- If `python` is not found, install Python 3.11+ and ensure the Python launcher/PATH is configured. If imports fail, activate `.venv` and run `python -m pip install -e ".[dev]"` again.
- If port 8000 is occupied, pass `--port 8001` to uvicorn.
- The default message limit is 4,000 characters (configurable from 1 to 20,000 with `JARVIS_MAX_MESSAGE_LENGTH`). The whole HTTP body is limited to 65,536 bytes by default (configurable from 1,024 bytes to 1,048,576 bytes with `JARVIS_MAX_REQUEST_BODY_BYTES`). The message character limit is checked after JSON parsing; the body byte limit is applied while the body is received.
- Provider timeout defaults to 5 seconds and is configurable from greater than 0 up to 60 seconds with `JARVIS_REQUEST_TIMEOUT_SECONDS`.
- `JARVIS_ENVIRONMENT` accepts `development`, `test`, or `production`; `JARVIS_LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, or `ERROR`. `JARVIS_APP_NAME` sets the API title. Only `JARVIS_PROVIDER=mock` is currently supported.
# Jarvis_AI
"# Jarvis_AI" 
