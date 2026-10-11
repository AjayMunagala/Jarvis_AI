# JARVIS Core v1.2

JARVIS is a modular foundation for a personal AI operating system. Core v1.2 adds single-owner authentication for optional persistence and owner-scoped memory access while preserving explicit memory controls and offline mock chat.

## What works now

- `GET /health` and `GET /ready` report service status and version.
- `POST /chat` accepts a validated message and returns a structured response. When persistence is enabled, it creates or continues a conversation and returns its stable `conversation_id`; without persistence, the ID is `null`.
- `GET /conversations/{conversation_id}/messages` reads a bounded page of stored user/assistant messages in chronological order. It accepts `limit` (1–100, default 50) and `before_sequence` for older pages.
- `/memories` provides explicit create, list, retrieve, update, delete, and ranked lexical search over separately stored records when PostgreSQL persistence is enabled. Every persistent route requires the configured single-owner bearer credential; memory queries enforce the configured owner UUID.
- Chat memory retrieval is separately controlled by `JARVIS_MEMORY_RETRIEVAL_ENABLED` (default `false`) and returns at most `JARVIS_MEMORY_RETRIEVAL_LIMIT` records (default 3, maximum 5). Retrieved records are framed as untrusted reference data in provider context.
- The default `mock` provider works without an API key, internet access, GPU, or paid service. It echoes the received message with a clear demo label; it does not reason like an LLM.
- An optional OpenAI Chat Completions adapter can be selected with `JARVIS_PROVIDER=openai`. It uses the existing provider interface and configured connect/read timeouts. Live OpenAI access is optional and was not verified in this environment.
- Chat requests pass through deterministic intent classification, a validated bounded plan, and JSON-configured advisory expert definitions. `AnalysisExpert` and `ValidationExpert` have distinct configured instructions and typed structured output contracts. For a non-demo provider, the adapter requests strict JSON Schema output. Before sending, it removes string and array size-bound keywords and the documented unsupported composition keywords from a copy of the provider schema; local Pydantic validation still enforces the full lengths, collection limits, required fields, and semantic rules. Array size bounds are supported for standard OpenAI models but not for fine-tuned models, so they are omitted to keep the configurable model path compatible. The public `reply` is readable text rendered from validated structured fields. Structured shape does not guarantee semantic quality.
- In mock mode, specialist contracts contain the deterministic legacy demo response as `summary`, use `assessment_status="not_evaluated"`, and leave specialist collections empty. The mock does not follow instructions or perform structured reasoning.
- `/chat` retains `reply`, `provider`, and `demo`, and adds `partial` to indicate whether some configured expert steps failed while useful results remained. It returns an `X-JARVIS-Execution-ID` header for chat success, provider failure, and timeout responses.
- Successful expert outputs are joined in plan order. If the combined reply exceeds 40,000 characters, it is clipped to fit that limit and ends with a visible truncation marker; this does not change the `partial` flag, which reports expert failures.
- The expert/tool registries and audit-event contracts are typed. `jarvis/orchestration/config/tools.json` is intentionally empty and no tool executor or external action exists.
- Provider timeouts, provider failures, invalid requests, and configured message limits are handled explicitly.
- Settings are validated at startup and JSON logs avoid request contents and secrets.
- When persistence is enabled, database save failures return sanitized HTTP 503; persistence is not falsely reported as successful.
- Optional conversation persistence is disabled by default. When enabled, successful chat exchanges are saved transactionally through a repository boundary; PostgreSQL is not needed for normal startup or mock-mode tests.
- PostgreSQL connection attempts use `JARVIS_PERSISTENCE_CONNECT_TIMEOUT_SECONDS` (default 3, range 1–15); database connection failures are sanitized as HTTP 503.
- API errors use a JSON `{ "error": ..., "detail": ... }` envelope. Validation errors return 422, oversized requests return 413, unknown conversation IDs return 404, provider failures return 502, persistence failures or unavailable history return 503, provider timeouts return 504, and unexpected application errors return a generic 500.

## Verification status

Core v0.7 verification on Windows/Python 3.12.10: `.venv-dev\Scripts\python.exe -m pytest -v` passed 130 tests; `.venv-dev\Scripts\python.exe -m compileall -q jarvis tests` passed; `npm test -- --run` passed 16 frontend tests; `npm run build` succeeded; and `git diff --check` passed. The Python suite ran with loopback access because restricted sandbox socket initialization stalls FastAPI TestClient. Persistence repository, API paging, and transaction behavior were tested with controlled doubles. No `psql` executable or psycopg package was available here, so the migration was not applied and live PostgreSQL behavior remains unverified.

The optional live PostgreSQL integration suite is in `integration_tests/test_postgres_integration.py` and is excluded from the default `tests/` suite. It requires an explicit opt-in, the `persistence` extra, local loopback PostgreSQL, PostgreSQL client tools, and a protected `PGPASSFILE`; it creates and removes a uniquely named disposable database. See [database setup and integration testing](database/README.md#opt-in-postgresql-integration-tests). It must never be aimed at production or a database containing user data.

For the v0.8 review, the default Python suite passed 130 tests, the focused persistence and database-runner suites passed 29 tests, and the opt-in integration module passed all 11 tests: 5 URL-safety checks and 6 live PostgreSQL cases. `compileall`, 16 frontend tests, and the frontend production build passed. The live run used a local PostgreSQL 18 server and a protected `PGPASSFILE`; the fixture checks that the file exists but does not inspect its Windows ACL. The generated test database was removed during cleanup.

Core v0.9 verification on Windows/Python 3.12.10: `.venv-dev\Scripts\python.exe -m pytest -v` passed 143 tests; the focused `tests/test_memory.py` suite passed 13 tests; the opt-in PostgreSQL integration suite passed 12/12 (5 URL guard checks and 7 live database cases); `.venv-dev\Scripts\python.exe -m compileall -q jarvis tests integration_tests` passed; `npm test -- --run` passed 16 frontend tests; `npm run build` succeeded; and `git diff --check` passed. The live test used the local PostgreSQL 18 server and removed its generated database. Python runs used loopback access because restricted sandbox socket setup stalls FastAPI TestClient. The existing Starlette TestClient deprecation warning recommending `httpx2` remains.

## Not implemented yet

There are not seven independent specialist agents: all configured experts share one provider. Only AnalysisExpert and ValidationExpert use distinct structured output contracts; these do not guarantee correct reasoning. Mock mode returns explicit `not_evaluated` placeholders and does not perform model reasoning. Memory records are explicit and separate from conversation history. Core v1.2 gates persistent routes with a single configured owner bearer credential and scopes memory SQL by the configured owner UUID; it is not a multi-user identity system and must not be exposed to untrusted users or shared deployments. Search is deterministic PostgreSQL lexical full-text ranking, not semantic retrieval. Embeddings, pgvector, automatic memory extraction, retention scheduling, memory access history, and memory export controls are not implemented. No model-driven intent classification or planning, tool execution, approval system, account system, or external action integration exists. The optional OpenAI adapter and structured-output compatibility have not been live-verified here. Structured specialist requests use a bounded 8,192 completion-token allowance; ordinary text requests retain 1,024 tokens. Truncated structured output is a permanent sanitized failure. Pydantic still enforces bounds removed from the transmitted schema. The frontend is a local React interface only; the API never runs shell commands or external actions.

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

When persistence is enabled, `/chat`, `/conversations/*`, and `/memories/*` require `Authorization: Bearer <JARVIS_LOCAL_AUTH_TOKEN>`. Generate a high-entropy token and stable owner UUID locally using the commands above, then store them only in the ignored `.env`. Persistence fails startup if either is absent or invalid. Keep this service on a trusted local environment: this bearer credential is not safe over plain HTTP on untrusted networks, application owner predicates are not database row-level security, and XSS can access the in-memory frontend token. The prototype is not suitable for public or shared deployments.

Persistence is off by default. To enable it, install `.[persistence]`, set `JARVIS_PERSISTENCE_ENABLED=true`, and configure `JARVIS_PERSISTENCE_DATABASE_URL`, `JARVIS_LOCAL_AUTH_TOKEN`, and `JARVIS_OWNER_ID` in the ignored local `.env`. The token must be at least 32 visible ASCII characters; generate it locally with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Generate a UUID once with `python -c "import uuid; print(uuid.uuid4())"` and keep `JARVIS_OWNER_ID` stable. `JARVIS_PERSISTENCE_CONNECT_TIMEOUT_SECONDS` defaults to 3 and accepts 1–15. Do not put real credentials in `.env.example`. Then apply the checked-in migrations with `database\\install_db.bat` following [database/README.md](database/README.md). The app does not connect to PostgreSQL at startup; a database connection is first made when a persisted operation runs.

When enabled, a successful authenticated `/chat` request without `conversation_id` creates a new UUID conversation and atomically stores the submitted user message and final assistant reply. Supplying an existing `conversation_id` appends the exchange atomically; an unknown supplied ID returns sanitized HTTP 404 and is never replaced with a new conversation. The response adds `conversation_id` while retaining `reply`, `provider`, `demo`, and `partial`; it also adds `memory_context` (null when retrieval is disabled, otherwise an outcome and selected IDs only). With persistence disabled, ordinary chat still works and returns `conversation_id: null`; trying to continue an ID or read history returns sanitized HTTP 503. The history endpoint returns the most recent page in chronological order, capped at 100 messages, and includes `has_more` and `next_before_sequence` for fetching older pages. Conversation IDs are identifiers, not authentication: persistence-backed chat and history use the single configured bearer credential. Provider prompts, credentials, raw provider payloads, and orchestration/audit traces are not stored. Ordinary tests use doubles; real PostgreSQL behavior is not claimed unless separately verified.

## Explicit long-term memory (Core v0.9)

Memory records are distinct from conversation history and are created only through the memory API; chat messages and replies are never automatically saved as memories. Migration `0003_long_term_memories.sql` adds a UUID, content (1–4,000 characters), optional category and source reference, and creation/update timestamps. Deleting a memory is a hard delete. The local frontend now provides an explicit memory manager for listing, exact category filtering, lexical search, create, edit, and confirmed permanent deletion. Chat responses include only retrieval outcome and selected memory IDs when retrieval is enabled; the interface does not claim a historical memory snapshot. The API supports `POST /memories`, `GET /memories?limit=50&offset=0&category=project`, `GET /memories/{memory_id}`, `PATCH /memories/{memory_id}`, `DELETE /memories/{memory_id}`, and `POST /memories/search` with a JSON body such as `{"query":"project preference","limit":10,"offset":0}`. Search text is kept in the request body rather than the URL so access logs do not record it in the request path. List and search limits are 1–100; offsets are bounded. The optional list category filter is exact-match, and its total and pagination apply to the filtered records. Search uses PostgreSQL's `simple` text-search configuration and `ts_rank_cd` with deterministic timestamp/UUID tie-breakers; it matches lexical terms and is not semantic search.

Lifecycle behavior is explicit: duplicate content is allowed and each create gets a distinct ID; PATCH changes only supplied fields, `null` clears optional category/source reference, content cannot be cleared, and an empty PATCH is rejected. Every successful PATCH refreshes `updated_at`, including a patch that supplies an already-equal value. DELETE permanently removes a record; repeating a successful deletion returns 404. When chat retrieval is enabled, metadata-only diagnostics report success, empty result, or fallback with execution ID, elapsed milliseconds, and record count. They do not include the query, memory IDs/content, category, source reference, credentials, or exception details.

All memory endpoints require persistence, the persistence extra, migrations 0003 and 0004, and a valid bearer token; missing or invalid credentials return 401 with `WWW-Authenticate: Bearer`. The same gate applies to `/chat` and conversation history whenever persistence is enabled. Clients cannot choose the owner. Chat retrieval is independently disabled by default. To enable it, set `JARVIS_MEMORY_RETRIEVAL_ENABLED=true` and `JARVIS_MEMORY_RETRIEVAL_LIMIT` from 1 to 5 while persistence is enabled. Retrieved content is passed as clearly separated untrusted reference data. Retrieval errors fall back to ordinary chat with no retrieved records and are logged without memory text or exception details; the additive response metadata reports `fallback` with an empty ID list. With retrieval disabled, `memory_context` is null. No embedding generation, pgvector, automatic extraction, or semantic retrieval is included.

Core v1.1 verification: the full Python suite passed 147 tests, the focused memory suite passed 17 tests, compileall passed, the frontend suite passed 25 tests, the frontend production build succeeded, and `git diff --check` passed. Live PostgreSQL integration tests were not run because the opt-in flag, admin URL, and `PGPASSFILE` were not configured. No live OpenAI request was made.

Core v1.2 offline verification in the current working tree: `.venv-dev\Scripts\python.exe -m pytest -v` passed 157 tests; the focused API/memory suite passed 48 tests; compileall passed for `jarvis`, `tests`, and `integration_tests`; the frontend suite passed 30 tests and `npm run build` succeeded; `git diff --check` passed. The opt-in integration module ran 5 URL-guard tests and skipped 10 database-dependent tests because `JARVIS_RUN_POSTGRES_INTEGRATION`, the admin URL, and `PGPASSFILE` were not configured (PostgreSQL client tools were also absent from `PATH`). Migration 0004 upgrade behavior and live owner isolation remain unverified. No live OpenAI request was made.

Migration `0004_memory_ownership.sql` adds nullable `owner_id` without a default or backfill. Existing memory rows remain unowned and inaccessible through application APIs; there is no HTTP claim route. The SQL owner predicates are application-level controls, not PostgreSQL row-level security. A bearer token sent over plain HTTP is unsafe on untrusted networks, and XSS can expose the token while the frontend page is open. This remains a single-owner local prototype, not safe for public or shared deployment. Do not store passwords, API keys, or other secrets in memory.

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

Open the URL printed by Vite (normally `http://localhost:5173`). To use a different backend origin with Vite, set `VITE_API_PROXY_TARGET` before starting the frontend, for example `$env:VITE_API_PROXY_TARGET = 'http://127.0.0.1:8001'`. `VITE_API_BASE_URL` can be set to an absolute API base URL for a direct browser connection; that requires the backend to allow the frontend origin through CORS. Neither variable is a secret. When a bearer token is active, the browser client only sends it to the configured API origin over HTTPS or loopback HTTP and rejects redirects; never configure a public or untrusted endpoint. The UI sends messages to the backend-selected provider and labels each reply as mock/demo or model output using the API response metadata; conversation view state remains in memory for the open page, while backend persistence may retain history when enabled.

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
- Conversation persistence is opt-in and PostgreSQL-backed. It supports client-supplied continuation IDs and a bounded read-only history API. All persistence-backed routes require one configured bearer credential; memory records use application-level owner predicates, not PostgreSQL row-level security. This prototype is not suitable for shared/public deployments.
- `/ready` confirms that settings and the configured provider were constructed; it does not probe external dependencies. Setting `JARVIS_ENVIRONMENT=production` only changes the validated environment label and does not add TLS, rate limiting, or broader production hardening.
- If `python` is not found, install Python 3.11+ and ensure the Python launcher/PATH is configured. If imports fail, activate `.venv` and run `python -m pip install -e ".[dev]"` again.
- If port 8000 is occupied, pass `--port 8001` to uvicorn.
- `--reload` works with Uvicorn's stat-based fallback when `watchfiles` is absent. Installing the optional `uvicorn[standard]` extra enables WatchFiles-based reload; the base dependency is sufficient to run the application.
- The default message limit is 4,000 characters (configurable from 1 to 20,000 with `JARVIS_MAX_MESSAGE_LENGTH`). The whole HTTP body is limited to 65,536 bytes by default (configurable from 1,024 bytes to 1,048,576 bytes with `JARVIS_MAX_REQUEST_BODY_BYTES`). The message character limit is checked after JSON parsing; the body byte limit is applied while the body is received.
- Messages longer than the configured limit, including messages beyond the 20,000-character schema ceiling, return 413. Other schema-invalid requests, including whitespace-only messages and extra fields, return 422.
- Provider timeout defaults to 5 seconds and is configurable from greater than 0 up to 60 seconds with `JARVIS_REQUEST_TIMEOUT_SECONDS`.
- `JARVIS_ENVIRONMENT` accepts `development`, `test`, or `production`; `JARVIS_LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, or `ERROR`. `JARVIS_APP_NAME` sets the API title. `JARVIS_PROVIDER` accepts `mock` or `openai`.
- `JARVIS_MAX_TOOL_ROUNDS` is bounded to 1–5 (default 5), `JARVIS_MAX_EXPERT_RETRIES` to 0–3 (default 2), and `JARVIS_MAX_PARALLEL_EXPERTS` to 1–3 (default 2). Execution is sequential by default; parallel plans must be explicitly declared in the expert configuration. Retries apply only to explicitly marked transient expert failures and share the configured request deadline.
