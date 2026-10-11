# JARVIS Core v0.6

JARVIS is a modular foundation for a personal AI operating system. Core v0.6 adds opt-in PostgreSQL persistence for a single `/chat` request/response exchange. The provider-independent interface and offline mock provider remain available.

## What works now

- `GET /health` and `GET /ready` report service status and version.
- `POST /chat` accepts a validated message and returns a structured response.
- The default `mock` provider works without an API key, internet access, GPU, or paid service. It echoes the received message with a clear demo label; it does not reason like an LLM.
- An optional OpenAI Chat Completions adapter can be selected with `JARVIS_PROVIDER=openai`. It uses the existing provider interface and configured connect/read timeouts. Live OpenAI access is optional and was not verified in this environment.
- Chat requests pass through deterministic intent classification, a validated bounded plan, and JSON-configured advisory expert definitions. `AnalysisExpert` and `ValidationExpert` have distinct configured instructions and typed structured output contracts. For a non-demo provider, the adapter requests strict JSON Schema output. Before sending, it removes string and array size-bound keywords and the documented unsupported composition keywords from a copy of the provider schema; local Pydantic validation still enforces the full lengths, collection limits, required fields, and semantic rules. Array size bounds are supported for standard OpenAI models but not for fine-tuned models, so they are omitted to keep the configurable model path compatible. The resulting public `reply` is readable text rendered from validated structured fields; the `/chat` response fields remain unchanged. Structured shape does not guarantee semantic quality.
- In mock mode, specialist contracts contain the deterministic legacy demo response as `summary`, use `assessment_status="not_evaluated"`, and leave specialist collections empty. The mock does not follow instructions or perform structured reasoning.
- `/chat` retains `reply`, `provider`, and `demo`, and adds `partial` to indicate whether some configured expert steps failed while useful results remained. It returns an `X-JARVIS-Execution-ID` header for chat success, provider failure, and timeout responses.
- Successful expert outputs are joined in plan order. If the combined reply exceeds 40,000 characters, it is clipped to fit that limit and ends with a visible truncation marker; this does not change the `partial` flag, which reports expert failures.
- The expert/tool registries and audit-event contracts are typed. `jarvis/orchestration/config/tools.json` is intentionally empty and no tool executor or external action exists.
- Provider timeouts, provider failures, invalid requests, and configured message limits are handled explicitly.
- Settings are validated at startup and JSON logs avoid request contents and secrets.
- When persistence is enabled, database save failures return sanitized HTTP 503; persistence is not falsely reported as successful.
- Optional conversation persistence is disabled by default. When enabled, successful chat exchanges are saved transactionally through a repository boundary; PostgreSQL is not needed for normal startup or mock-mode tests.
- PostgreSQL connection attempts use `JARVIS_PERSISTENCE_CONNECT_TIMEOUT_SECONDS` (default 3, range 1–15); database connection failures are sanitized as HTTP 503.
- API errors use a JSON `{ "error": ..., "detail": ... }` envelope. Validation errors return 422, oversized requests return 413, provider failures return 502, persistence failures return 503 when enabled, provider timeouts return 504, and unexpected application errors return a generic 500.

## Verification status

Core v0.6 verification on Windows/Python 3.12.10: `.venv-dev\Scripts\python.exe -m pytest -v` passed 117 tests; compileall passed; `npm test -- --run` passed 16 frontend tests; `npm run build` succeeded; and `git diff --check` passed. The suite ran with loopback access because restricted sandbox socket initialization stalls FastAPI TestClient. PostgreSQL repository/transaction behavior was tested with controlled connection doubles. No `psql` executable or psycopg package was available here, so the migration was not applied and live adapter/database behavior was not verified.

## Not implemented yet

There are not seven independent specialist agents: all configured experts share one provider. Only AnalysisExpert and ValidationExpert use distinct structured output contracts; these do not guarantee correct reasoning. Mock mode returns explicit `not_evaluated` placeholders and does not perform model reasoning. Persistence stores each request as its own two-message conversation; there is no client conversation ID, history endpoint, multi-turn linking, persistent memory, or audit/provider prompt storage. No model-driven intent classification or planning, tool execution, approval system, account system, or external action integration exists. The optional OpenAI adapter and structured-output compatibility have not been live-verified here. Structured specialist requests use a bounded 8,192 completion-token allowance; ordinary text requests retain 1,024 tokens. Truncated structured output is a permanent sanitized failure. Pydantic still enforces bounds removed from the transmitted schema. The frontend is a local React interface only; the API never runs shell commands or external actions.

## Prerequisites

- Windows 10/11 and Python 3.11 or newer (developed with Python 3.12.10).
- PowerShell.
- Node.js 22.12 or newer for the frontend (tested with Node 24.14.1).
- No GPU, API credentials, or internet are needed for default mock mode. Selecting OpenAI requires internet access and a valid API key.

## Install on Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

To install the optional PostgreSQL driver, add the persistence extra:

```powershell
python -m pip install -e ".[dev,persistence]"
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

## Optional conversation persistence

Persistence is off by default. To enable it, install `.[persistence]`, set `JARVIS_PERSISTENCE_ENABLED=true`, and set `JARVIS_PERSISTENCE_DATABASE_URL` in the ignored local `.env` to a PostgreSQL URL. `JARVIS_PERSISTENCE_CONNECT_TIMEOUT_SECONDS` defaults to 3 and accepts 1–15. Do not put a real URL in `.env.example`. Then apply the checked-in migrations with `database\\install_db.bat` following [database/README.md](database/README.md). The app does not connect to PostgreSQL at startup; a database connection is first made when a chat exchange is saved.

When enabled, each successful `/chat` request stores exactly the submitted user message and the final assistant reply as a new conversation. The user and assistant rows are one transaction. If persistence fails, `/chat` returns sanitized HTTP 503 with its execution ID rather than reporting a successful saved response. The HTTP response fields remain unchanged. The repository can create conversations, append role-checked messages, and read messages in stable insertion order, but no history API or conversation ID input exists yet, so requests are not linked across turns. Provider prompts, credentials, raw provider payloads, and orchestration/audit traces are not stored. Ordinary tests use doubles; real PostgreSQL behavior is not claimed unless separately verified.

The standalone provisioning foundation still creates/verifies the configured database and applies numbered migrations without destructive resets. See [database/README.md](database/README.md) for server, backup, and restore guidance.

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

Open the URL printed by Vite (normally `http://localhost:5173`). To use a different backend origin with Vite, set `VITE_API_PROXY_TARGET` before starting the frontend, for example `$env:VITE_API_PROXY_TARGET = 'http://127.0.0.1:8001'`. `VITE_API_BASE_URL` can be set to an absolute API base URL for a direct browser connection; that requires the backend to allow the frontend origin through CORS. Neither variable is a secret. The UI sends messages to the backend-selected provider and labels each reply as mock/demo or model output using the API response metadata; conversation history is only kept in memory for the open page.

Run the frontend checks from `frontend/`:

```powershell
npm test -- --run
npm run build
```

## Select demo mode

Mock mode is the default. Set `JARVIS_PROVIDER=mock` in `.env` to make it explicit. To use OpenAI, set `JARVIS_PROVIDER=openai`, add `JARVIS_OPENAI_API_KEY` to the local `.env` file, and optionally configure `JARVIS_OPENAI_BASE_URL` and `JARVIS_OPENAI_MODEL`. The base URL defaults to `https://api.openai.com/v1`, and the model defaults to `gpt-4o-mini`. Connect/read timeout values are configurable with `JARVIS_PROVIDER_CONNECT_TIMEOUT_SECONDS` and `JARVIS_PROVIDER_READ_TIMEOUT_SECONDS`; the existing overall request deadline still bounds each expert call. Missing OpenAI credentials fail startup; the service never falls back to mock after a real-provider failure. A real provider sends message content to the configured endpoint and may incur usage charges. `.env` is ignored by Git; never put a real key in `.env.example`.

## Where future capabilities belong

HTTP contracts live in `jarvis/api/routes/`; request and response types live in `jarvis/models/`; validated settings and shared infrastructure live in `jarvis/core/`; model adapters and provider selection live in `jarvis/intelligence/`; intent, plans, registries, expert execution and audit contracts live in `jarvis/orchestration/`. Expert and tool definitions are data in `jarvis/orchestration/config/`. Future capability modules should depend on small provider/tool interfaces and be registered at the orchestration boundary, rather than importing a specific model SDK. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Known limitations and troubleshooting

- The mock response is deterministic text, not model output, and does not emulate specialist reasoning.
- The analysis specialist is instructed to explain relevant factors, trade-offs, and supported conclusions. The validation specialist is instructed to check completeness, consistency, assumptions, and missing information. These are two instructed personas using the shared provider, not independent agent implementations or guaranteed expert judgments.
- `mock` and `openai` are the only supported providers. OpenAI selection fails startup without a nonblank API key. OpenAI live connectivity and credentials were not verified here.
- OpenAI refusal responses, malformed completion payloads, and empty or whitespace-only content are treated as sanitized provider failures; they are not retried as transient errors and do not switch to mock mode.
- Conversation persistence is opt-in and PostgreSQL-backed. It stores one request/response exchange per conversation; it is not long-term memory and has no frontend history or retrieval API.
- `/ready` confirms that settings and the configured provider were constructed; it does not probe external dependencies. Setting `JARVIS_ENVIRONMENT=production` only changes the validated environment label and does not add authentication, rate limiting, or production hardening.
- If `python` is not found, install Python 3.11+ and ensure the Python launcher/PATH is configured. If imports fail, activate `.venv` and run `python -m pip install -e ".[dev]"` again.
- If port 8000 is occupied, pass `--port 8001` to uvicorn.
- `--reload` works with Uvicorn's stat-based fallback when `watchfiles` is absent. Installing the optional `uvicorn[standard]` extra enables WatchFiles-based reload; the base dependency is sufficient to run the application.
- The default message limit is 4,000 characters (configurable from 1 to 20,000 with `JARVIS_MAX_MESSAGE_LENGTH`). The whole HTTP body is limited to 65,536 bytes by default (configurable from 1,024 bytes to 1,048,576 bytes with `JARVIS_MAX_REQUEST_BODY_BYTES`). The message character limit is checked after JSON parsing; the body byte limit is applied while the body is received.
- Messages longer than the configured limit, including messages beyond the 20,000-character schema ceiling, return 413. Other schema-invalid requests, including whitespace-only messages and extra fields, return 422.
- Provider timeout defaults to 5 seconds and is configurable from greater than 0 up to 60 seconds with `JARVIS_REQUEST_TIMEOUT_SECONDS`.
- `JARVIS_ENVIRONMENT` accepts `development`, `test`, or `production`; `JARVIS_LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, or `ERROR`. `JARVIS_APP_NAME` sets the API title. `JARVIS_PROVIDER` accepts `mock` or `openai`.
- `JARVIS_MAX_TOOL_ROUNDS` is bounded to 1–5 (default 5), `JARVIS_MAX_EXPERT_RETRIES` to 0–3 (default 2), and `JARVIS_MAX_PARALLEL_EXPERTS` to 1–3 (default 2). Execution is sequential by default; parallel plans must be explicitly declared in the expert configuration. Retries apply only to explicitly marked transient expert failures and share the configured request deadline.
