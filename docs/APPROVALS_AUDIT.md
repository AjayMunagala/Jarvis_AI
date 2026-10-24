# Approval gateway and audit trail (v2.9)

v2.9 records approval requests for actions JARVIS refused to perform, lets the owner approve or reject them, and keeps one central, owner-scoped audit trail for tool calls, browser actions and approval decisions.

**Approvals are recorded only.** Approving a request changes its state and is audited. Nothing is executed, in this version or later, unless a separately reviewed isolation design is accepted. The `ApprovalRequest.execution` field is the literal `"not_available"`, and no state represents execution.

## Where approval requests come from

| Source | Trigger | Category / risk |
|---|---|---|
| Tool runtime (`jarvis/tools/runtime.py`) | A call to a registered tool that is not LOW-risk and read-only (status `approval_required`) | `tool` / the tool's risk (`medium`, `high`, `critical`) |
| Browser service (`jarvis/browser/service.py`) | A click classified `consequential` (submit, payment, delete, and similar) | `browser` / `consequential` |

The runtime adds `approval:AR-…` to the `ToolResult.evidence`. The browser adds the approval id to the action record's reason. In both cases the action is **not** performed. If the approval cannot be stored, the action is still refused, and the refusal does not include an id.

Approval requests come only from trusted server code. The model, page content and documents cannot create, approve or widen them.

## Lifecycle

`pending` → `approved` | `rejected` (owner decision, exactly once), or `pending` → `expired` (after `JARVIS_APPROVALS_TTL_SECONDS`, default 900, range 60–86400).

The lifecycle enforces these rules:

- A decision is a single conditional update: `WHERE id AND owner_id AND state='pending' AND expires_at > now`. Expired or already-decided requests return 409. Unknown ids, and ids that belong to another owner, return 404.
- Identical pending requests are deduplicated. The scope hash is `sha256(category|action|canonical arguments or element)`, and the arguments themselves are never stored.
- Each owner can have at most 100 pending requests. Requests over the limit are refused, and the refusal is audited.
- Text fields are redacted for secrets and length-bounded. Notes are limited to 300 characters.

## Storage (decision D7)

| `JARVIS_PERSISTENCE_ENABLED` | Audit / approvals | Labelled |
|---|---|---|
| `true` | PostgreSQL tables `jarvis_app.audit_events`, `jarvis_app.approval_requests` (migration `0006_audit_and_approvals.sql`) | `durable: true` |
| `false` (default) | In memory: 1000 audit events, 500 approvals; cleared on restart | `durable: false` |

If a durable audit write fails, the record is kept in memory with `durable: false`. The Activity page then says how many records could not be stored durably. A non-durable record is never reported as durable. Approval storage failures return 503, and an action whose approval cannot be stored is still refused.

Each audit record holds metadata only: category, event type, decision, status, subject, correlation id and at most 12 scalar details. A tool call's details include the argument SHA-256, never the arguments themselves. Audit records never contain page content or secrets.

Apply migration 0006 with `database\install_db.bat` before enabling persistence on an existing database.

## API

The owner bearer token is required for every endpoint. Capabilities are enforced server-side, and none of them grants execution.

| Method and path | Capability | Notes |
|---|---|---|
| `GET /activity?limit=1..200&category=tool\|browser\|approval` | `audit.read` | Newest first; includes `durable_storage` and a `storage_note` |
| `GET /approvals?limit=1..200&state=…` | `approvals.read` | Sweeps expired requests first; includes `execution_note` |
| `GET /approvals/{id}` | `approvals.read` | 404 if unknown or foreign |
| `POST /approvals/{id}/approve` | `approvals.read` + `approvals.decide` | Body `{ "note"?: string ≤300 }`; extra fields return 422; records only |
| `POST /approvals/{id}/reject` | `approvals.read` + `approvals.decide` | Same body |

## Frontend

The **Activity** view lists pending requests with their risk, target, reason, expected side effects and expiry. Each pending request has two buttons: **Approve (record only)** and **Reject**. Decided and expired requests appear in a separate list. The audit trail is labelled durable or not durable for each record.

## Tests

`tests/test_approvals_audit.py` covers:

- the lifecycle, single decision, expiry, 404 and 409 responses, deduplication and the pending limit;
- redaction, and safe failure when storage is unavailable;
- the durable-write fallback;
- PostgreSQL SQL shape and parameters through a fake connection;
- migration discovery;
- tool and browser approvals that run nothing;
- authentication and each capability returning 403;
- unchanged defaults.

`frontend/src/components/ActivityPanel.test.tsx` covers the UI.

Live PostgreSQL behavior for migration 0006 has **not** been run; it needs operator credentials.
