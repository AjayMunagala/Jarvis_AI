# JARVIS Capability Registry and Read-Only Tool Runtime (v2.7)

v2.7 implements master-reference Phase 4 ("safe read-only tools"): a typed capability registry, a runtime that
validates, authorizes, bounds, and audits tool calls, and a set of read-only tools that wrap existing JARVIS
services. It is **off by default** (`JARVIS_TOOLS_ENABLED=false`). The orchestrator does **not** call tools in
v2.7; tools run only when the authenticated owner invokes them. Model-proposed tool calls are v2.8.

## Contracts (`jarvis/tools/models.py`, contract version 1)

| Contract | Purpose |
|---|---|
| `CapabilityDescriptor` | Tool id/version, title, description, `risk`, `read_only`, `side_effects: "none"`, required permissions, input/output JSON schemas, timeout, output limit, per-request `available`/`availability_reason`, `invocable`, `autonomous` (true only when v2.8 chat tools are enabled and the tool is invocable). |
| `ToolCall` | Server-assigned `call_id`, tool id, correlation id, deadline. Clients cannot set ids or permissions. |
| `ToolResult` | `status` (`succeeded`, `failed`, `timed_out`, `unavailable`, `denied`, `invalid_input`, `partial`, `approval_required`), typed `output` (only on success), sanitized `error`, `evidence` references, timings, `retryable`, `output_bytes`, `audit_event_id`, `untrusted_content`. |
| `ToolAuditEvent` | Metadata-only record: ids, tool, actor, decision, status, error code, argument **names** and a SHA-256 digest of the arguments (never values), duration, evidence count, `durable: false`. |
| `RiskClass` | `low`, `medium`, `high`, `critical` (master reference §10). Only `low` + read-only can run in v2.7. |

`ApprovalRequest` is intentionally not implemented; approval handling belongs to v2.9.

## Registry

`jarvis/orchestration/config/tools.json` lists the registered tools. It is data only: a fixed allowlist in
`jarvis/tools/handlers.py` binds each id/version to Python code, so configuration cannot add executable behavior.
An entry without a matching handler (or with a different version) is reported unavailable and denied. The
existing orchestration `ToolRegistry` validation is unchanged: only `read_only: true`, `executable: false`,
`risk: "low"` entries load. `executable: false` means the orchestrator never invokes the tool autonomously
(`ToolRegistry.execution_enabled` stays `False`). A conformance test asserts that every published input/output
schema equals the handler's Pydantic schema.

## Tools

| Tool | Wraps | Permissions | Notes |
|---|---|---|---|
| `calculator` | new safe AST evaluator | `tools.invoke`, `chat.advisory` | numbers, `+ - * / // % **`, `abs/min/max/round/sqrt`; no names, attributes, strings; ≤200 chars, ≤100 nodes, exponent ≤1000, \|result\| ≤ 1e100 |
| `memory.search` | `MemoryRepository.search` | `tools.invoke`, `memory.read` | owner-scoped lexical search; ≤10 hits, 400-char excerpts; requires persistence; document hits mark the result untrusted |
| `repo.search` | `RepoIntelligenceService.search` | `tools.invoke`, `repo.read` | text or filename search; **regex not exposed**; ≤50 matches |
| `repo.symbols` | `RepoIntelligenceService.find_symbols` | `tools.invoke`, `repo.read` | ≤50 symbols |
| `git.status` | `RepoIntelligenceService.get_git_status` | `tools.invoke`, `repo.read` | branch, HEAD, file status only |
| `git.diff_summary` | same | `tools.invoke`, `repo.read` | per-file insertion/deletion counts; **no diff content** |
| `verification.plan` | `VerificationService.create_plan` | `tools.invoke`, `verify.read` | plans checks; `executed: false`; the plan is cached in the verification service's in-memory plan store exactly as `POST /verification/plan` does |
| `browser.observe` | `BrowserService.fetch_observation` | `tools.invoke`, `browser.read` | requires `JARVIS_BROWSER_ENABLED`; URL policy and DNS-rebinding protection apply; output is untrusted |

## Enforcement order (`jarvis/tools/runtime.py`)

1. Runtime enabled, else `unavailable` (`tools_disabled`).
2. Registered **and** bound to an allowlisted handler with the same version, else `denied` (`unknown_tool`).
3. Registry and handler both declare `low` risk and read-only, else `approval_required` — not executed.
4. Caller holds `tools.invoke` and every capability the tool requires (checked against the app's live policy;
   policy errors deny), else `denied`.
5. Arguments are a JSON object within `JARVIS_TOOLS_MAX_ARGUMENT_BYTES` and validate against the strict input
   model (`extra="forbid"`), else `invalid_input`.
6. The tool's dependency is configured, else `unavailable` with the reason.
7. Run on a bounded worker pool (`JARVIS_TOOLS_MAX_CONCURRENT`) with the tool timeout capped by
   `JARVIS_TOOLS_MAX_TIMEOUT_SECONDS`. A timed-out worker keeps its slot until it actually finishes.
8. Output must validate against the declared output model and fit `JARVIS_TOOLS_MAX_OUTPUT_BYTES`, else
   `failed`. Known secret patterns in output are redacted. Exceptions return a generic message; exception text is
   never exposed. URL-policy refusals return `denied` (`destination_blocked`).

Every call, including refusals, appends one audit event (in-memory ring of 500, cleared on restart, labelled
`durable: false`) and a metadata-only structured log line.

## API

| Route | Capability | Result |
|---|---|---|
| `GET /capabilities` | `tools.read` | `CapabilitiesResponse` with per-request availability and invocability |
| `POST /tools/calls` `{tool_id, arguments}` | `tools.invoke` (+ tool permissions) | `ToolResult`; runtime outcomes use HTTP 200 and `status` in the body |
| `GET /tools/audit?limit=1..200` | `tools.read` | `AuditPage` (`durable: false`) |

All routes require the owner bearer token (401 otherwise); missing route capabilities return 403. An optional
`X-Request-ID` header becomes the call's correlation id.

## Configuration

| Setting | Default | Range |
|---|---|---|
| `JARVIS_TOOLS_ENABLED` | `false` | requires `JARVIS_LOCAL_AUTH_TOKEN` and `JARVIS_OWNER_ID` |
| `JARVIS_TOOLS_MAX_CONCURRENT` | 2 | 1–4 |
| `JARVIS_TOOLS_MAX_TIMEOUT_SECONDS` | 20 | 1–30 |
| `JARVIS_TOOLS_MAX_OUTPUT_BYTES` | 32768 | 1024–131072 |
| `JARVIS_TOOLS_MAX_ARGUMENT_BYTES` | 8192 | 512–65536 |

## Frontend

The **Capabilities** view lists registered tools with risk, permissions, limits, availability reasons and whether
they can be invoked; lets the owner run an invocable tool with JSON arguments (validated by the server); and shows
the result status, output, evidence, untrusted-content warnings, and recent non-durable audit events.

## Unchanged safety controls

No shell execution, arbitrary Python, patch application, verification execution, or external actions. The
orchestrator remains tool-free (`ToolRegistry.execution_enabled == False`; experts have no `tool_ids`).
`verification_execution_enabled=False`; `external_action.execute` is not granted.

## Known limitations

- Audit records are in-memory only (durable PostgreSQL audit is v2.9, decision D7).
- A timed-out handler thread cannot be forcibly stopped; it is bounded by the wrapped service's own limits and
  keeps its concurrency slot until it ends.
- `partial` is defined in the contract but no v2.7 tool produces partial results.
- Availability is computed per request from configuration; it does not probe external services.
- pgvector/semantic retrieval is **not implemented** (deferred, decision D6); `memory.search` is lexical.

## Testing

`.venv-dev\Scripts\python.exe -m pytest tests/test_tools.py -v` (unit/API tests with in-process fakes, this
repository for repo/git tools, and the mock browser provider) and `npx vitest run` in `frontend/`
(`CapabilitiesPanel.test.tsx`, `tools-api.test.ts`).

> **v2.8 update.** With `JARVIS_CHAT_TOOLS_ENABLED=true`, chat may propose calls to invocable tools; every call still goes through this runtime. See `docs/ACTION_LOOP.md`.
