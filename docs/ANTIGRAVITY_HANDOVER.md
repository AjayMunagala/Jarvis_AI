# JARVIS technical handover: v3.0 → v4.0 (for Antigravity)

This document hands the JARVIS codebase over at the end of v3.0. It describes what exists, what it promises, what is deliberately disabled, what has and has not been verified, and where the next phases should start.

It assigns no v3.x or v4.0 work and does not implement any.

## 1. Baseline

| Item | Value |
|---|---|
| Repository | https://github.com/AjayMunagala/Jarvis_AI |
| Handover branch | `jarvis/integration-acceptance-v30` (local; **not pushed**). It contains v3.0 on top of `jarvis/integration-acceptance-v29` (`ef3ae95`). |
| History | v2.4 `abcb594` → v2.5 `5d615fc` → v2.6 `a0dc5be` → v2.7 `7ba5853` → v2.8 `5d70dbc` → v2.9 `ef3ae95` → v3.0 (this commit) |
| Owner acceptance | v2.4–v2.8: accepted and integrated. v2.9 and v3.0: implemented and integrated locally under the owner's instruction to continue through v3.0; **formal owner acceptance is pending**. |
| Shared checkout | `D:\Project_Jarvis` is still on `jarvis/integration-acceptance-v25`; it was not modified. Work happened in worktrees under `D:\Project_Jarvis_worktrees\`. |
| Runtime | Windows 10/11, Python 3.12 (`.venv-dev`), Node with Vite, React 18 and TypeScript. PostgreSQL through psycopg is optional. Playwright Chromium is used for the browser provider and E2E tests. |

## 2. Capabilities at v3.0

| Area | State | Key docs |
|---|---|---|
| Chat with orchestration and experts; mock or OpenAI-compatible provider | On | `docs/ARCHITECTURE.md` |
| Conversations and long-term memory, document ingestion (lexical), attributed context | Needs `JARVIS_PERSISTENCE_ENABLED=true` and PostgreSQL | `docs/HANDOFF.md`, README |
| Repository intelligence and coding analysis (static, review-only patches) | On; **patches are never applied** | `docs/CODING_INTELLIGENCE.md` |
| Verification planning (v2.5) | On; **execution off** (`verification_execution_enabled=False`) | `docs/ARCHITECTURE.md` (v2.5) |
| Browser intelligence (v2.6): sessions, observation, safe interaction, research | `JARVIS_BROWSER_ENABLED=false` by default; consequential actions are **never executed** | `docs/BROWSER_INTELLIGENCE.md` |
| Capability registry and read-only tool runtime (v2.7): 8 LOW-risk read-only tools | `JARVIS_TOOLS_ENABLED=false` by default | `docs/TOOL_RUNTIME.md` |
| Action loop (v2.8): chat proposes read-only tool calls, which are validated, executed and verified | `JARVIS_CHAT_TOOLS_ENABLED=false` by default | `docs/ACTION_LOOP.md` |
| Approval gateway and central audit (v2.9) | On; **approving is recorded only and executes nothing** | `docs/APPROVALS_AUDIT.md` |
| Voice and unified interface (v3.0): browser-native speech, opt-in | Offered (`JARVIS_VOICE_ENABLED=true`); the owner opts in per page session | `docs/VOICE_INTERFACE.md` |

The frontend has these views: Chat (with voice), Capabilities, Memory, Coding, Browser and Activity. Settings is a placeholder.

## 3. API surface

All endpoints are JSON. Unless marked public, each requires `Authorization: Bearer <JARVIS_LOCAL_AUTH_TOKEN>` and a server-side capability check. Chat and conversations become authenticated when persistence is enabled.

| Group | Endpoints |
|---|---|
| Health (public) | `GET /health`, `GET /ready`, `GET /interface/config` |
| Chat | `POST /chat` (response: `reply`, `provider`, `demo`, `partial`, `conversation_id`, `memory_context`, `context`, `tool_activity`) |
| Conversations | `GET /conversations`, `GET /conversations/{id}/messages` |
| Memory | `GET/POST /memories`, `POST /memories/ingest`, `POST /memories/search`, `GET/PATCH/DELETE /memories/{id}` |
| Repository | `GET /repo/inventory`, `POST /repo/search`, `GET /repo/symbols`, `GET /repo/git`, `GET /repo/config`, `GET /repo/tests`, `POST /repo/diagnostics` |
| Coding / verification | `POST /coding/analyze`; `POST /verification/plan`, `POST /verification/verify-patch`, `POST /verification/run` (refused while execution is disabled), `GET /verification/profiles` |
| Browser | `GET /browser/status`; `GET/POST /browser/sessions`; `GET/DELETE /browser/sessions/{id}`; `POST …/navigate`, `POST …/actions`; `GET …/screenshots/{sid}`, `GET …/audit`; `POST /browser/research`; `POST /browser/checks/plan`, `POST /browser/checks/run` |
| Tools | `GET /capabilities`, `POST /tools/calls`, `GET /tools/audit` |
| Activity / approvals | `GET /activity`, `GET /approvals`, `GET /approvals/{id}`, `POST /approvals/{id}/approve`, `POST /approvals/{id}/reject` |

The contracts are Pydantic v2 models with `frozen=True` and `extra="forbid"`; the frontend types in `frontend/src/services/api.ts` mirror them. Several fields are `Literal` constants that encode safety guarantees, and changing any of them is a deliberate contract change:

- `ApprovalRequest.execution = "not_available"`;
- `InterfaceConfig.voice.server_receives_audio = false` and `auto_send = false`;
- `features.approval_execution = false`;
- `CapabilityDescriptor` `executable: false`.

The frontend calls the backend through the Vite `/api` proxy (`VITE_API_PROXY_TARGET`).

## 4. Module boundaries

| Package | Responsibility | Depends on |
|---|---|---|
| `jarvis/core` | Settings (`JARVIS_*`, validated), logging | none |
| `jarvis/security` | `Capability` enum, `CapabilityPolicy` (owner-only; never grants `external_action.execute`), auth helpers | core |
| `jarvis/api` | FastAPI routes and dependencies (`authenticated_owner`, `enforce_capability`) | all services |
| `jarvis/intelligence` | Model providers (mock, OpenAI-compatible) | core |
| `jarvis/orchestration` | Expert registry, tool registry (`tools.json`, `execution_enabled=False`), orchestration run | intelligence |
| `jarvis/memory`, `jarvis/persistence` | Conversations and memory repositories; PostgreSQL helpers with operation deadlines | core |
| `jarvis/repo`, `jarvis/coding`, `jarvis/verification` | Read-only repository analysis, review-only patches, verification planning and contracts | core |
| `jarvis/browser` | `BrowserService`, URL policy, `GuardedTransport` (DNS-rebinding protection), mock, HTTP and Chromium providers | security |
| `jarvis/tools` | Allowlisted handlers and `ToolRuntime` (enforcement order in `docs/TOOL_RUNTIME.md`) | the services above |
| `jarvis/agent` | `ActionLoop`, proposers, evidence rendering, answer verification | tools |
| `jarvis/audit` | Audit and approval contracts, memory and PostgreSQL stores, services | persistence helpers |
| `frontend/src/components`, `frontend/src/voice` | Views and panels; `SpeechProvider` interface plus the Web Speech implementation | `services/api.ts` |

Wiring lives in `jarvis/main.py`, through `create_app(settings)` and `app.state.*`. Tests construct apps through `create_app` with explicit `Settings(_env_file=None, …)`.

## 5. Configuration

`.env.example` is the authoritative list, and each entry is commented there. The safety-relevant defaults are:

| Setting | Default | Meaning |
|---|---|---|
| `JARVIS_PROVIDER` | `mock` | Deterministic demo replies; real providers need configuration |
| `JARVIS_PERSISTENCE_ENABLED` | `false` | PostgreSQL off; audit and approvals are in memory and labelled not durable |
| `JARVIS_BROWSER_ENABLED` | `false` | Browser off. `JARVIS_BROWSER_ALLOW_PRIVATE_NETWORK=false` |
| `JARVIS_BROWSER_CHECKS_ENABLED` | `false` | Browser check execution off |
| `JARVIS_TOOLS_ENABLED` | `false` | Tool runtime off |
| `JARVIS_CHAT_TOOLS_ENABLED` | `false` | Chat tool use off; it also requires tools to be enabled |
| `JARVIS_APPROVALS_TTL_SECONDS` | `900` | Pending approval expiry (60–86400) |
| `JARVIS_VOICE_ENABLED` | `true` | Offer opt-in browser voice controls |
| `verification_execution_enabled` | `False` (code) | Verification never executes commands |

`JARVIS_LOCAL_AUTH_TOKEN` (at least 32 characters) and `JARVIS_OWNER_ID` identify the single owner. Without them, authenticated endpoints return 401.

## 6. Database migrations

Migrations live in `database/scripts/`, applied by `database/runner.py` (`database\install_db.bat`). The runner records a ledger in `jarvis_meta.schema_migrations`, takes a SHA-256 of each file with line endings normalized, and refuses checksum drift and psql meta-commands.

| ID | Purpose |
|---|---|
| `0001_migration_metadata` | Migration ledger only |
| `0002_conversation_persistence` | Conversations and messages (`jarvis_app`) |
| `0003_long_term_memories` | Explicitly managed long-term memories |
| `0004_memory_ownership` | Owner isolation; pre-existing rows stay unowned and inaccessible |
| `0005_memory_source_kind` | `memory` versus `document` source kind; no backfill |
| `0006_audit_and_approvals` | `audit_events` and `approval_requests` (check constraints; no execution state) |

The **live application of 0006 has not been run**, because it needs operator PostgreSQL credentials. Migrations 0001–0005 were verified earlier through the opt-in PostgreSQL integration suite. pgvector is **not** installed or used (decision D6).

## 7. Security model and invariants

These invariants are covered by tests. Do not weaken any of them without a separately reviewed design and explicit owner approval.

1. **Deny by default; permissions enforced server-side.** `CapabilityPolicy` grants only the listed owner capabilities. `external_action.execute` and `verify.execute` are never granted. Neither the model, page content nor documents can grant permissions.
2. **No consequential execution.** There is no shell execution, no arbitrary Python, no automatic patch application, no verification or browser check execution, and no consequential browser action (submit, payment, delete and similar). Approving a request executes nothing.
3. **Tools are LOW-risk and read-only only.** `ToolRuntime` re-checks risk, read-only status, permissions, schema, availability, time, concurrency and output size on every call. Model-proposed calls (D3) go through the same path.
4. **Untrusted content is labelled.** Browser observations, documents, memory and tool output reach the model as clearly separated untrusted data. Prompt-injection pages are tested.
5. **Bounded resources.** Every subsystem has request-size, message, timeout, output, session, action, navigation, page, element and pending-approval limits.
6. **Sanitized records and errors.** Audit records hold metadata only, with secrets redacted and no arguments or page content. Errors are generic and never include credentials, SQL or stack traces.
7. **Honest reporting.** No tool operation is reported as successful without evidence. Non-durable records are labelled. Verification states are explicit (`verified`, `partially_verified`, `unverified`, `not_applicable`), and so are browser outcomes (`approval_required`, `blocked`).
8. **Fail safe.** If a policy or authorization check fails, or a store cannot record an approval, the request is denied or refused.
9. **Voice privacy.** Voice is opt-in for each session, with a disclosure. JARVIS never receives audio. Transcripts are reviewed before sending and never sent automatically.

The prototype is single-owner and local. It is **not** approved for shared or public deployment: the transport is plain HTTP, and the token is held in page memory.

## 8. Verification at handover

The results below are for v3.0 on Windows, run at the handover commit.

| Check | Result |
|---|---|
| Python tests (`pytest`) | **758 passed, 17 skipped, 0 failed**. The skips are pre-existing: Windows lacks symlink privilege (WinError 1314). The run includes real Chromium browser tests against a loopback site. |
| Frontend unit tests (Vitest) | **126 passed** (13 files) |
| `tsc -b` and `npm run build` | Passed |
| Playwright E2E (real backend in mock mode plus Vite) | **11 passed**, including the voice scenario with a deterministic stand-in for the Web Speech API |
| `compileall`, `git diff --check`, secret pattern scan | Passed |
| Live Uvicorn smoke test (loopback, generated test token) | Passed: health; `/interface/config` public; `/activity` returns 401 without auth and 200 with it; a calculator tool call appears in the activity trail; an unknown approval returns 404 |

Evidence for v2.9 was 753 passed and 17 skipped on Python, 105 Vitest and 10 E2E.

**Not run, or mock evidence only:**

- live PostgreSQL for migration 0006 and for the durable audit and approvals path. The PostgreSQL stores are tested only against a fake connection.
- live LLM providers, including real model tool proposals and grounded answers.
- real microphones, browser speech services and voices.
- external websites: browser tests use mock and loopback sites only.
- multi-user and remote deployment.

## 9. Known limitations

- Single owner, local only, with no TLS. The token is kept in page memory and must be entered again after a reload.
- The in-memory audit (1000 events) and approvals (500) are lost on restart when persistence is off.
- Approvals are informational. Nothing consumes an approved request.
- Retrieval is lexical only; there is no semantic or vector search (D6).
- Voice depends on the browser: Firefox has no `SpeechRecognition`. Chromium sends recognition audio to the vendor's service, and voice has no offline engine.
- The Settings view is a placeholder.
- On Windows, 17 symlink-related tests skip unless symlinks are permitted.

## 10. Deferred items, for the owner to prioritize

These were explicitly deferred. They are listed for context, not assigned.

- Executing approved actions. This needs a separately reviewed isolation and sandbox design, scoped single-use approvals bound to `scope_sha256`, re-validation at execution time, and evidence-based reporting. Approvals are currently recorded only.
- pgvector and semantic retrieval (D6).
- Local or offline speech engines behind the existing `SpeechProvider` interface, and server-side speech, which would need a new privacy review.
- Medium-risk tools; any write-capable tool requires the approval execution design first.
- Multi-user identity, TLS, deployment hardening and a Settings UI.

## 11. Compatibility notes for the next phase

- All additive response fields are optional in the frontend types. `ChatResponse` omits `context` and `tool_activity` when they are null; keep it additive.
- `tools.json` is validated by the orchestration `ToolRegistry`, which rejects executable or non-LOW entries. `ToolRuntime` re-checks independently. Keep both checks.
- New audit categories need changes in three places: the model `Literal`, the migration check constraint and the frontend union.
- New migrations must use the next number (`0007_…`), contain SQL only, and leave applied files unedited, because the runner rejects checksum drift.
- Frontend panels follow one pattern: an `auth` prop, a generation counter that drops stale responses, and `key={sessionVersionRef.current}` so the panel resets on lock.
- Speech providers implement `SpeechProvider` in `frontend/src/voice/speech.ts`. Keep transcripts going into the draft (`auto_send=false`) unless the owner approves a different design.
- Git practice so far: use per-version worktrees and local `jarvis/integration-acceptance-vNN` branches; do not push without explicit authorization; and never run destructive Git commands on owner checkouts.
