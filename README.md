# JARVIS Core v0.2

JARVIS is a modular foundation for a personal AI operating system. Core v0.2 adds a bounded Mother Orchestrator behind the FastAPI chat endpoint while retaining the provider-independent interface and offline mock provider.

## What works now

- `GET /health` and `GET /ready` report service status and version.
- `POST /chat` accepts a validated message and returns a structured response.
- The default `mock` provider works without an API key, internet access, GPU, or paid service. It echoes the received message with a clear demo label; it does not reason like an LLM.
- Chat requests pass through deterministic intent classification, a validated bounded plan, and JSON-configured advisory expert definitions. The current mock adapter still returns the same deterministic demo text; expert personas do not provide separate model reasoning.
- `/chat` retains `reply`, `provider`, and `demo`, and adds `partial` to indicate whether some configured expert steps failed while useful results remained. It returns an `X-JARVIS-Execution-ID` header for chat success, provider failure, and timeout responses.
- Successful expert outputs are joined in plan order. If the combined reply exceeds 40,000 characters, it is clipped to fit that limit and ends with a visible truncation marker; this does not change the `partial` flag, which reports expert failures.
- The expert/tool registries and audit-event contracts are typed. `jarvis/orchestration/config/tools.json` is intentionally empty and no tool executor or external action exists.
- Provider timeouts, provider failures, invalid requests, and configured message limits are handled explicitly.
- Settings are validated at startup and JSON logs avoid request contents and secrets.
- API errors use a JSON `{ "error": ..., "detail": ... }` envelope. Validation errors return 422, oversized requests return 413, provider failures return 502, provider timeouts return 504, and unexpected application errors return a generic 500.

## Verification baseline

Verified on Windows with Python 3.12: `.venv\Scripts\python.exe -m pytest -v` reports 56 passed (one Starlette deprecation warning recommending `httpx2`); frontend tests report 15 passed; `npm run build` succeeds; and `git diff --check` is clean.

## Not implemented yet

There is no real LLM adapter, model-driven intent or planning, specialist expert reasoning, application database connection, persistent memory, tool execution, approval system, account system, or external integration. The planner and classifier are deterministic and bounded. Optional standalone PostgreSQL provisioning is documented below, but the app does not use it or persist conversations. The frontend is a local React interface only; the API never runs shell commands or external actions.

## Prerequisites

- Windows 10/11 and Python 3.11 or newer (developed with Python 3.12.10).
- PowerShell.
- Node.js 22.12 or newer for the frontend (tested with Node 24.14.1).
- No GPU or API credentials are needed for demo mode.

## Install on Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

If PowerShell blocks virtual environment activation, use `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that PowerShell session and activate again.

The declared PEP 517 build backend is `setuptools.build_meta` and requires `setuptools>=68`. With package-index access, `pip install -e ".[dev]"` installs the build requirement in its isolated build environment. In an offline setup, the required setuptools build dependency (and the project's declared runtime and development packages) must already be available to pip's build environment or in its local wheel cache. Wheel packaging has not been verified as part of this setup.

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
python -m pytest -v
```

## Optional PostgreSQL setup

The FastAPI app does not require PostgreSQL and does not persist chat data. The standalone, Windows-oriented database provisioning foundation creates the configured database when needed and tracks numbered migrations; its initial schema contains migration metadata only. See [database/README.md](database/README.md) for setup, verification, backup, and restore instructions.

## Run the web interface (Windows PowerShell)

The React interface lives in `frontend/` and talks to the existing FastAPI `/chat` endpoint. The Vite development server proxies `/api/*` to `http://127.0.0.1:8000`; no backend CORS change is needed for local development.

The page, icons, styles and fonts are bundled or use local system fonts; the interface does not fetch remote assets. npm needs access to the package registry only during dependency installation.

In one PowerShell window, start the backend:

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn jarvis.main:app --reload
```

In a second PowerShell window, install and start the frontend:

```powershell
cd frontend
npm install
npm run dev
```

Open the URL printed by Vite (normally `http://localhost:5173`). To use a different backend origin with Vite, set `VITE_API_PROXY_TARGET` before starting the frontend, for example `$env:VITE_API_PROXY_TARGET = 'http://127.0.0.1:8001'`. `VITE_API_BASE_URL` can be set to an absolute API base URL for a direct browser connection; that requires the backend to allow the frontend origin through CORS. Neither variable is a secret. The UI sends each message to the mock provider and displays its actual demo reply; conversation history is only kept in memory for the open page.

Run the frontend checks from `frontend/`:

```powershell
npm test -- --run
npm run build
```

## Select demo mode

Demo mode is the default. Set `JARVIS_PROVIDER=mock` in `.env` to make it explicit. Supported provider choices are validated; unsupported choices fail startup. `.env` is ignored by Git.

## Where future capabilities belong

HTTP contracts live in `jarvis/api/routes/`; request and response types live in `jarvis/models/`; validated settings and shared infrastructure live in `jarvis/core/`; model adapters and provider selection live in `jarvis/intelligence/`; intent, plans, registries, expert execution and audit contracts live in `jarvis/orchestration/`. Expert and tool definitions are data in `jarvis/orchestration/config/`. Future capability modules should depend on small provider/tool interfaces and be registered at the orchestration boundary, rather than importing a specific model SDK. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Known limitations and troubleshooting

- The mock response is deterministic text, not model output.
- Only the mock provider is implemented. Other provider values are rejected.
- In-memory chat state is not persistent. PostgreSQL setup scripts provide optional database creation and migration metadata only; application persistence and memory remain later milestones.
- `/ready` confirms that settings and the configured provider were constructed; it does not probe external dependencies. Setting `JARVIS_ENVIRONMENT=production` only changes the validated environment label and does not add authentication, rate limiting, or production hardening.
- If `python` is not found, install Python 3.11+ and ensure the Python launcher/PATH is configured. If imports fail, activate `.venv` and run `python -m pip install -e ".[dev]"` again.
- If port 8000 is occupied, pass `--port 8001` to uvicorn.
- `--reload` works with Uvicorn's stat-based fallback when `watchfiles` is absent. Installing the optional `uvicorn[standard]` extra enables WatchFiles-based reload; the base dependency is sufficient to run the application.
- The default message limit is 4,000 characters (configurable from 1 to 20,000 with `JARVIS_MAX_MESSAGE_LENGTH`). The whole HTTP body is limited to 65,536 bytes by default (configurable from 1,024 bytes to 1,048,576 bytes with `JARVIS_MAX_REQUEST_BODY_BYTES`). The message character limit is checked after JSON parsing; the body byte limit is applied while the body is received.
- Messages longer than the configured limit, including messages beyond the 20,000-character schema ceiling, return 413. Other schema-invalid requests, including whitespace-only messages and extra fields, return 422.
- Provider timeout defaults to 5 seconds and is configurable from greater than 0 up to 60 seconds with `JARVIS_REQUEST_TIMEOUT_SECONDS`.
- `JARVIS_ENVIRONMENT` accepts `development`, `test`, or `production`; `JARVIS_LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, or `ERROR`. `JARVIS_APP_NAME` sets the API title. Only `JARVIS_PROVIDER=mock` is currently supported.
- `JARVIS_MAX_TOOL_ROUNDS` is bounded to 1–5 (default 5), `JARVIS_MAX_EXPERT_RETRIES` to 0–3 (default 2), and `JARVIS_MAX_PARALLEL_EXPERTS` to 1–3 (default 2). Execution is sequential by default; parallel plans must be explicitly declared in the expert configuration. Retries apply only to explicitly marked transient expert failures and share the configured request deadline.
