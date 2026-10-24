# JARVIS v4.1–v5.0 program

This document defines what the v4.1–v5.0 versions contain, why, and how each one is accepted. No earlier document defined these versions. The master reference uses Phases 0–15, and the version numbers v3.1–v4.0 and v5.1–v5.6 come only from commit history. The scope below was therefore derived from the code itself, through an investigation of the v3-to-v5 candidate (`3a35dda`).

- **Branch:** `claude/jarvis-v4.1-to-v5.0`, created from `3a35dda`. That candidate contains v3.0, v3.1–v4.0 (Antigravity) and v5.1–v5.6 (Claude).
- **Original delivery branches:** never modified.

## Baseline at `3a35dda` (before v4.1)

| Suite | Result |
|---|---|
| Python | 1039 passed, 17 skipped (Windows symlink privilege) |
| Vitest | 131 passed; tsc and build pass |
| Playwright E2E | 11 passed |
| Live PostgreSQL | **Failed.** Migration 0008 could not install, so 32 setup errors. |

## Version map

| Version | Title | Problem it solves |
|---|---|---|
| **v4.1** | Foundation and safety repair | Storage could not be installed; two arbitrary-file operations; a cross-owner read; a crash; fake successes and a false citation |
| v4.2 | Correlation, audit consistency, truthful status | No request or correlation IDs. v3.x audits are always "tool / success" and failures are never audited. `/ready` ignores most subsystems. CPU and memory figures are fake. |
| v4.3 | Enforced approvals for consequential internal actions | Self-improvement apply, quarantine, autonomous non-dry-run and mutating workflow steps bypass `ApprovalService`. This changes the v2.9 "approvals recorded only" decision, so it needs owner approval at its gate. |
| v4.4 | Reliable workflow execution | No step timeouts. Cancel does nothing. Retries apply to non-idempotent steps, and compensation is not idempotent. Tool steps bypass `ToolRuntime`. Blocking sleeps run inside requests. |
| v4.5 | Bounded proactive runtime | The scheduler never runs on its own and reminders never fire. No duplicate suppression, quiet hours, or persisted throttle. |
| v4.6 | One knowledge and context contract | `hash()` "embeddings" change across restarts. There are three context budgeters. Research output never reaches the knowledge index or the graph. PDF/DOCX parsing needs a dependency decision. |
| v4.7 | Integrated assistant experience in chat | Chat uses none of v3.1–v4.0 or v5.1–v5.6, and the Assistant view is unreachable. The integration sits behind a flag, so default chat is unchanged. |
| v4.8 | Integration and device readiness | No adapter framework, enrollment, revocation or secret references. Only a mock adapter is planned; there is no real device control. |
| v4.9 | Controlled evolution hardening | Improvement proposals live in memory only and snapshots are not owner-scoped. No real evaluation runs before acceptance, and no regression detection. |
| v5.0 | Integration and release readiness | Combined regression, fresh-install and upgrade tests, frontend states, documentation, and the READY / NOT READY verdict |

**Dependencies:**
- v4.1 comes before all other versions.
- v4.2 → v4.3, v4.4, v4.5.
- v4.3 → v4.4, v4.5, v4.9.
- v4.6 → v4.7.
- v4.8 needs v4.2 and v4.3.
- v5.0 needs all of them.

**Shared-file and conflict watch-list:**
- Shared files: `jarvis/main.py`, `jarvis/security/policy.py`, `jarvis/audit/models.py`, the audit category check, `orchestration/config/tools.json`, `api/routes/chat.py`, `frontend/src/App.tsx` and `frontend/src/services/api.ts`.
- Audit category check: every new category needs a forward migration that keeps all existing categories.
- Numbering: new migrations for v4.x use **0060+**.

**Every version follows the same gate:**
1. Explain.
2. Design.
3. Implement.
4. Run the narrow tests, then all suites.
5. Run the live PostgreSQL suite whenever the schema or persistence changes.
6. Review.
7. Report.
8. Stop for owner acceptance.

Results are always labelled as real, mock or unavailable.

## Status ledger

| Version | Status | Evidence | Commit |
|---|---|---|---|
| v4.1 | ACCEPTED by the owner on 2026-10-24; pushed | Python 1071 passed; live PostgreSQL 51 passed | `64fb86d` |
| v4.2 | ACCEPTED by Claude as project lead on 2026-10-24 (acceptance delegated by the owner), after an independent review and fixes | Python 1093 passed; live PostgreSQL 54 passed; Vitest 131; E2E 11 | branch head at push |
| v4.3 | ACCEPTED by Claude as project lead on 2026-10-24 (owner approved the policy change the same day), after an independent review that found one blocking defect, since fixed | Python 1115 passed; live PostgreSQL 57 passed; Vitest 132; E2E 11 | branch head at push |
| v4.4 | ACCEPTED by Claude as project lead on 2026-10-24, after an independent review (no bypass found; 8 status-reporting and robustness findings, all fixed) | Python 1135 passed; live PostgreSQL 59 passed; Vitest 132; E2E 11 | branch head at push |
| v4.5 | ACCEPTED by Claude as project lead on 2026-10-24, after an independent review found one blocking defect (a run could resurrect a deleted job or rewind the schedule), since fixed with nine other findings | Python 1160 passed; live PostgreSQL 61 passed; frontend unchanged | branch head at push |
| v4.6 | ACCEPTED by Claude as project lead on 2026-10-24, after an independent review found three blocking defects in research indexing (fence lost on chunking, filename breaking the fence, a chunker that crawled), since fixed with eight other findings | Python 1182 passed; live PostgreSQL 63 passed; frontend unchanged | branch head at push |
| v4.7 | ACCEPTED by Claude as project lead on 2026-10-24, after an independent review (no blocking issues; 7 findings, all fixed) | Python 1194 passed; live PostgreSQL 63 passed; Vitest 132; E2E 11 | branch head at push |
| v4.8 | ACCEPTED by Claude as project lead on 2026-10-24, after an independent review (no blocking issues; 2 medium and 4 low findings, all fixed) | Python 1204 passed; live PostgreSQL 64 passed | branch head at push |
| v4.9 | ACCEPTED by Claude as project lead on 2026-10-24, after an independent review (no blocking issues; 5 medium and 4 low findings, all fixed) | Python 1226 passed; live PostgreSQL 65 passed | branch head at push |
| v5.0 | ACCEPTED by Claude as project lead on 2026-10-24. Verdict: READY to merge into main (owner merges; three owner decisions remain, listed in the v5.0 section) | Python 1230 passed; live PostgreSQL 67 passed; Vitest 132; tsc and build pass; E2E 11 passed; live Uvicorn smoke passed (all on clean 72af343) | `72af343` |

---

## v4.1 — Foundation and safety repair

### Why it was needed
The investigation found that the combined code base could not be installed on PostgreSQL. It also found two operations that let any authenticated owner move or overwrite files anywhere on disk. Every later version depends on safe, installable storage, so these were fixed first.

### Changes

1. **Migrations 0008–0013 can now be installed.**
   - **The defect:** each of these migrations declared `owner_id ... REFERENCES jarvis_app.users(id) ON DELETE CASCADE`, but no migration creates `jarvis_app.users`. JARVIS identifies its single owner by a configured UUID, and no code uses a users table. A fresh install therefore failed at 0008.
   - **Why editing in place is safe:** the migrations failed before they could ever be recorded, so no database's ledger contains them.
   - **The approved fix:** the owner approved an in-place repair that removes only those clauses. `ON DELETE CASCADE` belongs to the same foreign-key clause and is not valid SQL on its own, so it was removed with it. The bare `approved_by ... REFERENCES jarvis_app.users(id)` was removed as well.
   - **Scope:** 15 clauses across 6 files.
   - **Proof that nothing else changed:**
     - `tests/fixtures/migrations_before_v41/` holds byte-exact copies of the six original files.
     - The tests check that each repaired file equals its original with only those clauses removed.
     - The tests also check that all 12 other migrations are byte-identical (their checksums are pinned).
2. **Quarantine is confined to the repository** (`jarvis/health/quarantine.py`).
   - Paths go through the existing repository guard `safe_resolve_path`. It rejects anything outside the root, `..`, drive-relative paths, alternate data streams, and symlink or reparse-point components.
   - `.git` and the quarantine directory itself are refused, as are directories.
   - A restore never overwrites an existing file.
   - Error messages and audit records use repository-relative paths, and the audit record no longer stores the free-text reason.
   - Quarantine is a move plus a SHA-256 check, not encryption, and the documentation now says so.
3. **Self-improvement targets are allowlisted** (`jarvis/improvement/immutable.py`).
   - Only two kinds of target are allowed: the registered virtual prompt templates, and existing `.md`/`.txt` files under `prompts/` resolved through the repository guard.
   - Everything else is refused (HTTP 422), including absolute paths, drive letters, application code and the guard file itself.
   - The old denylist is kept as a second layer.
   - Nothing is created on disk; only existing allowlisted files are rewritten.
4. **Knowledge chunk reads are owner-scoped** in the store protocol, the memory store, the PostgreSQL SQL and every service call. This closes a cross-owner read through `/knowledge/context-budget`.
5. **Workflows no longer fake success** (`jarvis/workflow/service.py`).
   - **Before:** a missing service, an unknown tool or an unsupported action returned a placeholder "success" (`*_mock`, `tool_executed`, `assistant_executed`, `completed`).
   - **Now:** they raise `StepUnavailableError`. The step fails immediately with the reason and is not retried.
   - **Crash fixed:** the `knowledge.search` step now calls the real `search(owner_id, KnowledgeSearchRequest)`. Before, it raised `TypeError` every time and retried with blocking sleeps.
6. **Research citations are honest.** URLs are never fetched. Their citation now reads "Not fetched: URL recorded as a reference only…" instead of "verified by research scanner".

### API and database changes
- No new endpoints and no new tables.
- **Behaviour changes:**
  - `/health/security/quarantine` returns 422 for paths outside the repository.
  - `/improvement/proposals` returns 422 for non-allowlisted targets.
  - Unavailable workflow steps are reported as `failed` instead of `completed`.
  - The text of research URL citations changed.
- **Store protocol:** `KnowledgeStore.get_document_chunks(owner_id, document_id)`.

### Tests

**New**
- `tests/test_v41_foundation.py`: 32 tests. These cover migration exactness, quarantine and self-improvement containment (with a junction to exercise the reparse-point check), owner scoping, honest workflow failures, the working `knowledge.search` step, and research honesty.
- `integration_tests/test_v41_postgres_stores.py`: 8 live tests. They write through the API with persistence enabled and read back through a new app instance, for the assistant, workflow, knowledge (including the cross-owner check in SQL), research, scheduler, health and finance stores, plus unknown-ID handling.

**Updated**
Three existing quarantine tests used to quarantine files from the system temp folder, which is the unsafe behaviour removed here. They now use a temporary repository root, and what they check is unchanged.

### Known limitations (deferred to later versions)
- Quarantine and self-improvement approvals are still self-approval by the same owner. Resolved in v4.3.
- `/autonomous/execute` still defaults to `dry_run=False`. Resolved in v4.3: a command with write steps now waits for approval whatever the default.
- These defects remain open:
  - finance balance updates are not atomic;
  - `compensate` is not idempotent (v4.4);
  - the scheduler has no background runner (v4.5);
  - `hash()` embeddings (v4.6).
- Fake CPU and memory values and the always-healthy scheduler `health_check` remain until v4.2.

---

## v4.2 — Correlation, audit consistency and truthful status

### Why it was needed
- **No tracing.** A request could not be followed across logs, audit, tools and workflow steps.
- **Indistinguishable audit records.** Every v3.x service audited as `category="tool", decision="allow", status="success"`, so the trail could not be filtered by subsystem. Failures were never audited.
- **Invented status reports.** Several reported values were never measured:
  - CPU 15% and memory 42% in every health snapshot;
  - a "database: operational" label, without any check;
  - a scheduled `health_check` that always answered "all healthy";
  - an autonomous overview built from fixed feature claims.

### Changes

1. **Correlation IDs** (`jarvis/core/correlation.py`).
   - **Where the ID comes from:** a pure ASGI middleware gives every request an `X-Request-ID`. A client-supplied value is accepted only if it matches `[A-Za-z0-9._-]{1,64}`; anything else is replaced with a generated `req-<16 hex>`, so clients cannot inject header or log content.
   - **How code reads it:** the ID lives in a context variable. The thread pools that run blocking work copy that context, so services read it with `get_correlation_id()` without extra parameters.
   - **Where it is recorded:**
     - it is returned in the response header;
     - it is added to every JSON log line;
     - `AuditService.record` uses it whenever a caller gives no correlation ID, so every v3.x audit record now carries it automatically;
     - the tools route uses it instead of reading the raw header.
2. **Audit categories and vocabulary.**
   - Each v3.x subsystem audits under its own category: `assistant`, `workflow`, `knowledge`, `research`, `scheduler`, `health`, `finance` and `improvement` (self-improvement previously used `approval`).
   - Forward migration **0060** replaces the category check with the union of every earlier category plus these eight. 0006, 0058 and 0059 are unchanged, and a test checks that every category in the model is allowed by the migration.
   - All services use the standard vocabulary `decision="allowed"`, `status="succeeded"`.
3. **Failure auditing** (`jarvis/core/failure_audit.py`).
   - **What is recorded:** a failed POST, PUT, PATCH or DELETE on a v3.x route by an authenticated owner is audited as `request_failed`, or as `request_denied` for HTTP 403. Each record has `status="failed"` and `details.http_status`; it does not claim whether state changed, because that is not observed. The write runs in a worker thread, off the event loop.
   - **What is never recorded:** request bodies, query strings and error messages. The subject is the method plus the path with IDs replaced by `{id}`.
   - **Not covered:**
     - Unauthenticated requests (401) cannot be attributed to an owner, so they are not audited.
     - Subsystems that already audit their own failures (tools, browser, approvals, intelligence) are excluded, so nothing is counted twice.
4. **Truthful status** (`jarvis/status/service.py`, `GET /status`, capability `status.read`).
   - **States:** each subsystem reports `operational`, `limited`, `mock`, `disabled` or `unavailable`.
   - **Evidence:** each report lists the checks that actually ran for this request, such as a real `SELECT 1` with a timeout, and the documented limitations.
   - **Overall status:** only `unavailable` degrades the overall status.
   - **Safety:** database errors are sanitized and the URL is never reported.
   - **Unchanged:** the public `/health` and `/ready` endpoints.
5. **Real telemetry.** CPU usage is sampled for 0.2 seconds and memory is read from the operating system, through the Windows API via `ctypes` or `/proc` on Linux. No new dependency was added. A value that cannot be measured is `null`, never an invented number. Forward migration **0061** makes the three snapshot columns nullable.
6. **Status reuse.**
   - Health snapshots record the real `{subsystem: state}` map instead of fixed labels.
   - A scheduled `health_check` reports the real states. It fails if no status source is connected.
   - A scheduled `workflow` job now fails as "not connected" (planned for v4.5) instead of claiming it was triggered.
   - The autonomous overview keeps its per-owner counts. Its fixed claims ("Hybrid BM25 + Vector RRF", "operational" text) are replaced with each subsystem's state and limitations.

### API and database changes
- **New:** `GET /status` (authenticated, `status.read`), the `X-Request-ID` header on every response, and audit categories (migration 0060).
- **Changed fields:**
  - Telemetry fields can be `null` (migration 0061).
  - Audit records from v3.x services use their own categories and the standard vocabulary.
  - Health snapshot `services_status` holds the real state map.
- **Changed scheduler behaviour:** a `workflow` job fails honestly.

### Tests
- **New:**
  - `tests/test_v42_observability.py`: 17 tests.
  - `integration_tests/test_v42_postgres.py`: 3 live tests. They cover a real `SELECT 1` status check, durable storage of the new categories and failure records with their correlation IDs, and nullable telemetry.
- **Updated to the truthful behaviour:**
  - the assistant audit test now expects category `assistant`;
  - the scheduler test now expects an unconnected health check to fail;
  - the v4.1 migration test now allows new migrations numbered 0060 and above.

### Independent review and acceptance
An independent reviewer read the full v4.2 diff and found no blocking issues. It confirmed header validation, per-request context isolation, middleware order, owner attribution, no double auditing, that 0060 keeps every earlier category, that 0061 is safe, and that `/status` leaks no secrets. Six of its seven findings were fixed before acceptance, each with a test:

1. The autonomous `diagnostic.run` step crashed when disk space could not be measured. It now reports "not measured".
2. The failure-audit database write ran on the event loop. It now runs in a worker thread.
3. Unhandled 500 errors lost the correlation ID, because Starlette's error handler runs outside the middleware. The handler now reads the ID from request state, logs it and returns it in `X-Request-ID`.
4. `details.mutated=false` was asserted, not observed. It was removed.
5. Linux telemetry could raise an uncaught `IndexError` on a malformed `/proc/meminfo` line, and counted guest CPU time twice. Both are fixed.
6. `/status` reported PostgreSQL-backed subsystems as operational while the database was unreachable. They are now `unavailable`, with the reason.

**Accepted as a known limitation:** a failed read-style POST, such as `/knowledge/search`, is audited as a failed request. This is extra audit volume, not a false claim.

**Also noted:** `chat` still reports `operational` without a live probe, and `provider` reports `operational` without contacting the provider (the limitation says so). v4.7 revisits both when chat is connected to the services.

**Tests after the fixes:** 5 new tests (22 in `tests/test_v42_observability.py`). Python: 1093 passed, 0 failed, 17 skipped (symlink privileges). Live PostgreSQL: 54 passed. Live Uvicorn smoke: correlation header echoed, a failed mutation audited with its ID, real telemetry, and the token absent from the log. The frontend was not changed by the fixes (Vitest 131, E2E 11 from the v4.2 run).

## v4.3 — Enforced approvals

### Owner decision
On 2026-10-24 the owner decided that writes from the autonomous coordinator, controlled improvement and quarantine must wait for the owner's approval before they run. This replaces the v2.9 decision that approvals are recorded only, for these actions. Tool and browser approvals are unchanged: they are still recorded only.

### How it works
1. **First call.**
   - You call a gated action as before.
   - The gateway records a pending approval request whose `scope_sha256` binds the exact action and target.
   - The response is **HTTP 202** with an `approval_required` body naming the approval ID. Nothing runs.
   - Asking again for the same action returns the same pending request.
2. **Approval.** You approve the request with `POST /approvals/{id}/approve`, from the Activity panel or the API. Approving still runs nothing by itself.
3. **Run once.**
   - Repeating the original call with `approval_id=<id>` marks the request used and then runs the action.
   - The request is marked used in one conditional update, so an approval can never run its action twice, even under concurrent calls.
   - The outcome is audited as `approved_action_succeeded` or `approved_action_failed`.
4. **Refused with HTTP 409, with nothing run:**
   - a pending, rejected or expired request;
   - a request that was already used;
   - a request approved for a different action;
   - a target that changed after approval;
   - a request approved longer ago than the approval window (`approvals_ttl_seconds`, 15 minutes by default).
5. **Fail closed.** If the gateway is missing, or cannot store the request, the result is HTTP 503 and nothing runs.

### What is gated and what the approval is bound to
| Action | Endpoint | Bound to |
|---|---|---|
| Autonomous command with write steps | `POST /autonomous/execute` (`approval_id` in the body) | prompt, context, `max_steps` and every planned step with its payload |
| Improvement apply | `POST /improvement/proposals/{id}/decide` with `approved` (`?approval_id=`) | proposal, target, and hashes of the original and proposed content |
| Improvement restore | `POST /improvement/proposals/{id}/restore` (`?approval_id=`) | proposal, snapshot, and hashes of the current and snapshot content |
| Quarantine | `POST /health/security/quarantine` (`?approval_id=`) | repository path, file SHA-256 and reason |
| Quarantine restore | `POST /health/security/quarantined/{id}/restore` (`?approval_id=`) | record, file SHA-256 and path |

### What still runs without approval
- **Read-only autonomous steps:** `finance.accounts.list`, `knowledge.search` and `improvement.benchmarks.run`. Every other step, including `health.diagnostic.run`, which stores a snapshot, counts as a write.
- **Dry runs.**
- **Rejecting an improvement proposal.**
- **Invalid requests:** a missing file, a path outside the repository, a stale improvement target or an unrestorable record is refused before any approval is requested.

### Database and API changes
- **Migration 0062:**
  - adds the categories `autonomous`, `improvement` and `quarantine`;
  - adds a `consumed_at` column, with a check that it can be set only on an approved request, after its decision;
  - leaves existing rows unchanged.
- **API:**
  - Approval records gain `consumed_at`, and `execution` is now `single_use` for the enforced categories and `not_available` for tool and browser.
  - Autonomous results gain `approval_id`.
  - The capabilities flag `approval_execution` stays `false`, because it describes tool and browser approvals.
- **Activity panel:** enforced requests show "Approve (allows one run)". After approving, it explains how to run the action and shows whether each approval was used.

### Not covered
- Mutating workflow steps are not gated yet. Workflows run only when the owner starts them, and tool steps fail as unavailable. Gating moves to v4.4, with the reliable step runner, and to v4.5, before the scheduler can start workflows on its own.

### Independent review and acceptance
An independent reviewer found no way to bypass the gate. It confirmed:
- every caller of the gated methods;
- deterministic scope material;
- the atomic consume in both stores;
- migration 0062's compatibility with the constraints from 0006;
- error mapping, owner scoping and audit content.

It also found one blocking defect and four smaller issues, all fixed before acceptance:

1. **Blocking.** Autonomous prompts longer than 480 characters crashed with HTTP 500, because the approval's reason field holds 500 characters. The approval fields are now truncated, and a test uses the longest allowed prompt.
2. **Misleading audit.** An approved autonomous run whose steps failed was audited as succeeded. The audited outcome now follows the command's real status.
3. **Approval wasted on restore.** A quarantine restore that could not succeed used up its approval first. A non-mutating precheck now refuses it before any approval is requested.
4. **Double apply.** Two approvals for the same improvement proposal could both apply it. Apply and restore now re-check the proposal under a lock after the approval is used.
5. **Test gaps.** New tests cover the store-level approval window and a live PostgreSQL race, in which eight threads try one approval and exactly one succeeds.

**Tests:**
- `tests/test_v43_approvals.py`: 22 tests.
- `integration_tests/test_v43_postgres.py`: 3 live tests, covering durability across restart, the database constraints and the race.
- Seven earlier tests were updated to go through the approval flow.
- Python: 1115 passed, 0 failed, 17 skipped (symlink privileges).
- Live PostgreSQL: 57 passed.
- Vitest: 132. E2E: 11. TypeScript and the build pass.
- Live Uvicorn smoke: 202 with nothing written, 409 before approval, 200 with exactly one write after approval, 409 on reuse, and the token absent from the log.

## v4.4 — Reliable workflow execution

### Why it was needed
- **No time limits:** steps ran without a timeout and runs without a deadline.
- **Cancel did nothing:** it changed the status, and the still-running steps then overwrote it.
- **Unsafe retries:** every failure was retried, including writes, which could duplicate a write that partly happened. Retries slept inside the request with `time.sleep`.
- **Tool steps never ran:** they bypassed the tool runtime and always failed.
- **Untrue compensation:**
  - Manual compensation guessed each step's action from its name.
  - It could run twice.
  - It reported writes it could not undo as "compensated".
- **Non-atomic finance:** a transaction and its balance change were two separate writes, so a failure could leave them out of step.

### Changes
1. **Bounded execution.**
   - Each step runs in a worker pool with a timeout, counted from when the step actually starts (default 30 s, `JARVIS_WORKFLOW_STEP_TIMEOUT_SECONDS`).
   - Each run has a deadline (default 300 s).
   - A write step that times out is never retried. It is reported with an unknown outcome (`manual_required`), because a Python thread cannot be stopped.
   - A step that never got a worker is reported as "did not start, wrote nothing".
2. **Safe retries.**
   - Only read-only steps are retried.
   - Retry waits use the run's cancel event instead of `time.sleep`.
3. **Working cancel.**
   - A live run stops before its next step, or during a retry wait.
   - Completed write steps are kept and listed, so the owner can undo them with compensate.
   - A run is shown as cancelled only if the cancel actually skipped a step.
   - Cancelling a run with no live runner, such as one left over from a restart, only applies while the run is still running or pending.
4. **Approval.**
   - A run with write steps needs an approved request (category `workflow`, migration 0063). The approval is bound to the definition, every step and the inputs.
   - Read-only runs need no approval.
   - The classification follows the action, not the step type. For example, a `tool` step with `finance.accounts.create` is a write.
   - In-process tool handlers count as writes unless they are declared read-only.
5. **Background runs.**
   - `wait: false` starts a run and returns at once.
   - At most `JARVIS_WORKFLOW_MAX_ACTIVE_RUNS` runs (default 4) are active. When the limit is reached, the response is 409, before any approval is used.
6. **Tool steps** run through the read-only `ToolRuntime` and its policy.
7. **Honest, idempotent compensation.**
   - It uses the stored definition.
   - It skips steps it already undid.
   - A write with no automatic undo, or with an unknown outcome, is reported as `manual_required`.
   - Research document parsing gained an undo (the document is deleted).
   - Only failed or cancelled runs can be compensated; any other run gets 409.
8. **Atomic finance.**
   - A transaction and its balance change are written in one database transaction.
   - Deleting a transaction twice reverses the balance only once.
9. **Restart recovery.**
   - At startup, a background task marks runs left `running` by the previous process as failed ("Interrupted").
   - It only touches runs created before the process started, and only if they are still running.

### API and settings
- **New request fields:** `approval_id` and `wait` on `POST /workflows/definitions/{id}/run`.
- **New responses:**
  - 202 when a run needs approval;
  - 409 when the engine is full;
  - 409 when compensating a run that is not failed or cancelled.
- **New settings:** `JARVIS_WORKFLOW_STEP_TIMEOUT_SECONDS`, `JARVIS_WORKFLOW_RUN_TIMEOUT_SECONDS` and `JARVIS_WORKFLOW_MAX_ACTIVE_RUNS`.
- **Behaviour change:** synchronous runs also count toward the active-run limit.

### Independent review and acceptance
The reviewer found no way to run a write step without approval. It confirmed:
- the read-only classification matches the dispatch;
- approval binding;
- no deadlock between the run and step pools;
- correct context-variable propagation;
- the finance SQL.

It found eight issues, all fixed with tests before acceptance:
1. A step queued behind hung steps was reported as "outcome unknown" although it never ran.
2. A cancel that raced a finishing run could overwrite "completed" with "cancelled", making it eligible for compensation.
3. Startup recovery could mark a run that had just finished as failed.
4. Compensation said "compensated" while an unknown-outcome write existed.
5. A full engine used up the approval before refusing.
6. In-process tool handlers were treated as read-only.
7. A cancel arriving after the last step reported the run as cancelled.
8. A failed final status write could leave a run "running".

**Tests:**
- `tests/test_v44_workflow_reliability.py`: 20 tests.
- `integration_tests/test_v44_postgres.py`: 2 live tests, covering atomic finance with a once-only reversal, and an approved run plus restart recovery.
- Python: 1135 passed, 0 failed, 17 skipped.
- Live PostgreSQL: 59 passed.
- Vitest: 132. E2E: 11.

These were run on the v4.4 commit in a separate clean checkout.

**Known limits:**
- A step thread that times out keeps running until its call returns.
- Recovery covers the configured owner's first 200 runs per state.

## v4.5 — Bounded proactive runtime

### Why it was needed
- **Nothing ran on its own:** jobs ran only when triggered by hand, and reminders were never delivered.
- **Cron and schedule errors were hidden:** every cron expression ran hourly, and an invalid interval silently became 5 minutes.
- **The throttle was unreliable:** its state was lost on restart, and it could drop reminders.
- **No duplicate protection.**
- **Workflow jobs were not connected** to the workflow engine.

### Changes
1. **Strict schedules** (`jarvis/scheduler/cron.py`).
   - A real five-field cron parser: lists, ranges, steps, and Sunday as 0 or 7. As in standard cron, a day field starting with `*` does not restrict the day.
   - Validation: intervals must be 60 seconds to 366 days; `once` takes a delay of 0 to 366 days or an ISO date-time.
   - An invalid schedule is refused with 422 when a job is created or enabled. Disabling a job never needs a valid schedule.
2. **Background runner** (`jarvis/scheduler/runner.py`).
   - It is off by default, following the execution-disabled defaults. Turn it on with `JARVIS_SCHEDULER_RUNNER_ENABLED=true`; it ticks every `JARVIS_SCHEDULER_TICK_SECONDS`, 30 s by default.
   - Each tick delivers due reminders, runs due jobs and releases held notifications.
   - A failed tick is logged; `/status` shows the runner state and the failures in a row.
3. **Each due run happens at most once.**
   - A due job is claimed with a compare-and-set on `next_run_at`, so only one worker runs it, even across processes.
   - A run then records its result with another compare-and-set, never by rewriting the whole row. A job deleted, disabled or edited during its run keeps that change, and a slow run never rewinds the schedule.
   - Missed runs after downtime collapse into one.
   - The concurrency limit is checked before claiming, so a busy scheduler leaves the job due.
   - At startup, jobs left `running` by a crash are marked failed.
4. **Reminders.**
   - A due reminder is delivered once: its notification is stored first, under an ID derived from the reminder, and only then is the reminder marked triggered.
   - A storage failure leaves the reminder pending for the next tick.
   - Reminders are never dropped by the anti-fatigue throttle.
5. **Quiet hours.**
   - Set them with `JARVIS_SCHEDULER_QUIET_HOURS` (for example `22:00-07:00`) and `JARVIS_SCHEDULER_UTC_OFFSET_MINUTES`.
   - During quiet hours, non-urgent notifications are stored but held. They are hidden from listings and released when the quiet hours end, by the runner or on the next listing.
   - The throttle still applies, and duplicate held notifications collapse.
6. **Persisted throttle:** the last delivery time is read from storage, so a restart does not reset the throttle. Throttled notifications are audited.
7. **Workflow jobs.**
   - Workflow jobs start a background run of the workflow engine.
   - A workflow with write steps cannot run unattended, because approvals are single-use. The job records one approval request, sends one notification, pauses itself and runs nothing.

### Independent review and acceptance
The reviewer confirmed:
- the claim is a correct compare-and-set;
- cron parsing is correct;
- no write workflow runs without approval;
- reminders are delivered once;
- quiet hours wrap around midnight correctly;
- no content appears in audit records or logs.

It found one blocking defect, fixed before acceptance: a job run wrote back a stale copy of the whole job. That could re-create a deleted job, re-enable a disabled one, revert an edit, and rewind the schedule so a due run happened twice across workers.

It also found nine more issues, all fixed with tests:
1. A due run was lost when the concurrency limit was reached.
2. A crash between claim and run left the job "running".
3. Quiet hours bypassed the throttle, so notifications flooded in the morning.
4. A reminder was lost when storing its notification failed.
5. A recurring write workflow asked for approval on every tick.
6. Cron treated `*/1` as restricting the day.
7. `once` values `nan`, `inf` and `1e20` caused HTTP 500.
8. Older jobs with now-invalid schedules could not be disabled.
9. Smaller issues:
   - scheduler queries had no statement timeout;
   - `/status` stayed "limited" after a single failed tick;
   - long workflow runs delayed reminders.

**Tests:**
- `tests/test_v45_scheduler_runtime.py`: 26 tests.
- `integration_tests/test_v45_postgres.py`: 2 live tests, covering the claim race across six workers, and once-only reminders with hold, release and the persisted throttle.
- Python: 1160 passed, 0 failed, 17 skipped.
- Live PostgreSQL: 61 passed.

These were run on the v4.5 commit in a separate clean checkout. The frontend is unchanged.

**Known limits:**
- Notifications appear in JARVIS only; nothing is sent to other devices.
- Delivery is at most once: a run claimed by a process that then crashes is not retried.

## v4.6 — One knowledge and context contract

### Why it was needed
- **Embeddings went stale on every restart:** they used Python's `hash()`, which changes from process to process, so stored vectors were noise after a restart.
- **Two token estimators** gave different counts, 4 and 3 characters per token.
- **The knowledge budget ignored the shared packer:** it used its own greedy packer instead of the v5.3 context packer.
- **Research output reached neither the knowledge index nor the graph.**
- **PDF and DOCX uploads were stored as garbage:** they were decoded as UTF-8 text.
- **The search audit stored the raw query text.**

### Changes
1. **Stable embeddings.**
   - BLAKE2b hashing (`stable-hash-64-v2`) gives the same vector in every process.
   - In PostgreSQL, a vector stored under the old model is recomputed in memory for the search. It is then stored in a separate, best-effort transaction, so the search itself stays read-only.
   - Semantic search scans the most recent 5000 chunks, in order.
2. **One token estimator** (`jarvis.routing.requirements.estimate_tokens`, about 3 characters per token) for every budget.
3. **One context contract.**
   - The knowledge context budget packs chunks with the shared v5.3 packer: deduplication, the token budget and excerpts.
   - It reports `omitted_count`, `truncated_count` and `untrusted_content`.
   - Explicitly chosen documents keep their chunk order.
   - At most 30 chunks are selected per request.
4. **Research to knowledge and graph** (`index_in_knowledge`, opt-in).
   - The parsed text is indexed after the research document is saved. Every chunk is marked `untrusted` (migration 0064), because a fence around the whole document would not survive chunking.
   - The document is recorded as a graph entity.
   - Deleting the research document removes its knowledge copy and its graph entity.
5. **Parsing.**
   - DOCX is parsed with a streaming expat parser, with these limits:
     - 5 MB of XML;
     - text capped at the extraction limit;
     - any DTD refused, in any encoding.
   - PDF is refused with a clear message. It needs a PDF library, and adding one is a new dependency that needs your approval. **Owner decision pending.**
   - Other binary uploads are refused.
   - Pre-extracted text is always accepted.
   - Invalid base64 is answered with 422.
   - The fence header can no longer be closed by a crafted filename or content type.
6. **Privacy:** the knowledge search audit records only the query length.

### Independent review and acceptance
The reviewer confirmed:
- embedding stability;
- owner scoping;
- bounded decompression;
- budget arithmetic;
- that other callers of the packer are unchanged;
- that the search audit no longer leaks the query.

It found three blocking defects, all in the new indexing, fixed before acceptance:
1. Untrusted research text lost its fence once chunked. An injected instruction in a middle chunk reached context as ordinary, pinned knowledge.
2. A crafted filename could close the fence. This was pre-existing, and v4.6 copied it further.
3. The chunker advanced one character at a time at line and document ends, producing dozens of tiny junk chunks. This was also pre-existing.

It also found eight more issues, all fixed with tests:
1. The graph entity was left behind on delete.
2. Partial failures could leave orphaned knowledge copies.
3. Embedding writes ran inside the search transaction.
4. The chunk order changed for explicitly chosen documents.
5. DOCX parsing could use excessive memory and CPU.
6. A UTF-16 DOCTYPE bypassed the DTD check.
7. Text that merely looked like PDF or ZIP was refused.
8. Tests that proved less than they claimed.

**Tests:**
- `tests/test_v46_knowledge_contract.py`: 26 tests.
- `integration_tests/test_v46_postgres.py`: 2 live tests, covering legacy vectors that are recomputed and replaced, and durable research indexing with cleanup.
- Python: 1182 passed, 0 failed, 17 skipped.
- Live PostgreSQL: 63 passed.

These were run on the v4.6 commit in a separate clean checkout.

**Behaviour changes:**
- Token counts are about 33% higher (3 instead of 4 characters per token).
- The context budget selects at most 30 chunks.
- Uploads of PDF or other binary files now return 422 instead of storing garbage.
- Chat context assembly (v2.2) keeps its character budget. v4.7 adds knowledge to chat through the shared records.

## v4.7 — Integrated assistant experience in chat

### Why it was needed
Chat used none of the v3.1–v4.6 services. The model could not see the owner's tasks, reminders, calendar or knowledge store, even though all of them exist in JARVIS. The Assistant view was already reachable in this branch, so v4.7 did not need to change it.

### Changes
- **Workspace context, opt-in.** It is controlled by `JARVIS_CHAT_WORKSPACE_CONTEXT_ENABLED`, which is off by default, so default chat is unchanged. When it is on, chat adds two kinds of record to the model's context:
  - **Knowledge records (`K` refs):** the best-matching knowledge-store chunks. Chunks from untrusted documents are labelled `untrusted document: …`.
  - **Workspace records (`W` refs):** one-line summaries of open tasks, reminders due in the next 48 hours, and calendar events in the next 48 hours.
- **Read-only.** Chat creates, changes or deletes nothing, and building the context writes no audit records.
- **Capabilities.** Each source is used only with its read capability (`knowledge.read`, `assistant.read`).
- **Bounded and safe.**
  - Gathering uses at most half of the remaining request time and stops between queries once that is spent.
  - A failing or slow source is skipped and reported (`workspace_status`); it never fails the chat request.
  - Stateless chat without an owner is unchanged.
- **Untrusted data.**
  - Records are rendered with the rest of the context as untrusted JSON data.
  - The system instruction explains K and W refs, including that W titles may have been created by automations.
  - Citations accept K and W refs only when such records were supplied.
- **Budget order.**
  1. memories;
  2. conversation history;
  3. documents;
  4. knowledge;
  5. workspace summaries, last, with at most 8 slots.
- **Visibility.** The chat UI and `/status` show whether workspace context is on, and the UI says when it was unavailable.

### Independent review and acceptance
The review found no blocking issues. It confirmed:
- owner isolation;
- correct capability checks, which fail closed;
- that untrusted text cannot escape the JSON data block;
- that workspace text is never stored in conversations or memory;
- additive API contracts.

It found seven issues, all fixed with tests before acceptance:
1. Workspace summaries could crowd out relevant documents.
2. A timed-out gathering kept a worker thread busy.
3. Stateless chat returned an empty context object when the flag was on.
4. Each chat turn wrote a search audit row.
5. Literal "[K1]" text counted as an unknown citation when the feature was off.
6. W records were not described as possibly coming from automations.
7. Test gaps.

Pre-existing, outside v4.7: `MemoryAssistantStore.list_tasks` can fail when stored due dates mix naive and timezone-aware values. Chat now treats naive dates as UTC; the store fix is left for v5.0.

**Tests:**
- `tests/test_v47_chat_workspace.py`: 12 tests.
- Python: 1194 passed, 0 failed, 17 skipped.
- Live PostgreSQL: 63 passed.
- Vitest: 132. E2E: 11.

These were run on the v4.7 commit in a separate clean checkout.

**Not covered:** chat still uses the configured provider, not the v5.1 model router.

## v4.8 — Integration and device readiness

### Why it was needed
There was no safe way to connect anything outside JARVIS: no adapter framework, no enrollment or revocation, and no way to refer to a credential without storing it. As planned, only a mock adapter is provided; no real device is controlled.

### Changes
1. **Adapter framework** (`jarvis/integrations`).
   - An adapter declares each of its actions as read-only or state-changing, and validates the arguments.
   - Only `mock.lamp` is registered: a simulated lamp whose state lives in memory.
   - A real adapter must implement the same interface and needs its own review before it is registered.
2. **Enrollments** move from `pending` to `active` to `revoked`.
   - Activation needs an approved request (category `integration`), bound to the enrollment, adapter and secret reference.
   - Revocation is immediate, idempotent and final.
   - A revoke waits for an action that is already running and wins for every later one; revoke and actions are serialized per enrollment.
3. **Actions.**
   - A read-only action runs directly on an active enrollment.
   - A state-changing action needs an approved request bound to the enrollment, the action and its exact validated arguments.
   - An approval for one enrollment or action never covers another.
4. **Secret references.** An enrollment stores a reference, `env:JARVIS_INTEGRATION_<NAME>`, never the value.
   - The value is resolved only when an action runs.
   - It is never stored, returned, logged or audited.
   - The API shows only `present`, `missing` or `not_required`.
   - The variable must be a real process environment variable; values in `.env` are not exported.
   - The required prefix stops a reference from naming other settings, such as the auth token or the database URL.
5. **API and database.**
   - Endpoints:
     - `/integrations/adapters`;
     - `/integrations/enrollments` (list and create);
     - `.../{id}` (get);
     - `.../{id}/activate`;
     - `.../{id}/revoke`;
     - `.../{id}/actions`.
   - New capabilities: `integrations.read`, `integrations.manage` and `integrations.act`.
   - Migration 0065 adds the enrollment table with state and format checks, and the `integration` audit and approval categories. Every earlier category is kept.
   - `/status` reports integrations as `mock`.

### Independent review and acceptance
The reviewer confirmed:
- the conditional state transitions;
- approval binding;
- the secret-reference pattern, including newline, Unicode and case bypass attempts, and its database check;
- that no secret appears in logs, audit records or output;
- input bounds and error mapping;
- the capability split;
- the migration.

It found two medium issues:
1. A revoke during an in-flight action did not stop it, and the action reported success.
2. `/status` showed the wrong storage.

It also found four low issues, and every finding was fixed with tests before acceptance:
- an unregistered adapter broke revoke;
- the `.env` behaviour was undocumented;
- a docstring named the wrong migration;
- owner-isolation and approval-binding tests were too weak.

**Tests:**
- `tests/test_v48_integrations.py`: 10 tests.
- `integration_tests/test_v48_postgres.py`: durable lifecycle and the database checks.
- Python: 1204 passed, 0 failed, 17 skipped.
- Live PostgreSQL: 64 passed.

These were run on the v4.8 commit in a separate clean checkout.

**Known limit:** the revoke/action lock is per process. A multi-process deployment would also need a database row lock.

## v4.9 — Controlled evolution hardening

### Why it was needed
- **Lost on restart:** improvement proposals, restore snapshots and changes to the built-in prompts lived in memory only.
- **Snapshots were not owner-scoped.**
- **Nothing was evaluated before acceptance:** the "benchmarks" were trivial checks that nothing called before approval, with no regression detection.

### Changes
1. **Durable, owner-scoped state** (migration 0066).
   - Proposals, with their evaluation, are stored durably.
   - Restore snapshots can be read only by their owner.
   - Changes to virtual prompt targets are stored. That table is deliberately global, like file-backed prompts.
2. **Every proposal is evaluated against the content it would replace.** These are offline static checks: no model is run, and meaning or negation is not checked.
   - **Override phrases:** a fixed list, matched after Unicode normalisation (NFKC, case folding, invisible characters removed, whitespace collapsed) and compared by count.
   - **Rule lines:** every rule line of the current content must remain as a whole line.
   - **Two trivial size checks.**
   - A score lower than the current content's is a *regression*. A check that already failed before is only a warning, so no target is ever frozen.
3. **Approval.**
   - A regressing proposal is refused (422) before any approval request is created, and it is evaluated again at decision time.
   - A change that only rewords or removes rule lines can be applied with an explicit `acknowledge_rule_changes`. The approval request then names the changed lines.
   - Override phrases can never be acknowledged.
4. **Consistency.**
   - Applying and restoring use conditional writes (compare-and-set on the content and on the proposal status), so two workers can never both apply competing changes.
   - Restore is refused once a later change exists, so it never silently undoes later approved work.
   - The evaluation stays on the proposal.
5. **Errors and content.** A storage outage returns 503. Prompt content keeps its exact whitespace.

### Independent review and acceptance
The reviewer confirmed:
- no apply or restore path bypasses the approval gate;
- approval binding to the content hashes;
- owner-scoped SQL;
- JSON handling;
- timeouts.

It found five medium issues and four low issues, all fixed with tests before acceptance.

Medium:
1. The phrase check was beaten by an extra space, a line break, a zero-width character or a synonym.
2. A rule could be "kept" inside a sentence that cancelled it.
3. False positives froze legitimate edits, with no override.
4. Restoring an old proposal silently reverted later approved changes.
5. With several workers, the process-local lock no longer protected anything.

Low:
- a storage outage returned 500;
- the global target table was undocumented;
- trailing whitespace was stripped from content;
- tests covered too little.

**Tests:**
- `tests/test_v49_controlled_evolution.py`: 26 tests.
- `integration_tests/test_v49_postgres.py`: durability across restart, owner-scoped snapshots, persisted prompts.
- Python: 1226 passed, 0 failed, 17 skipped.
- Live PostgreSQL: 65 passed.

These were run on the v4.9 commit in a separate clean checkout.

**Behaviour changes:**
- Approving a proposal can now return 422 when it regresses.
- Rewording a rule line needs `acknowledge_rule_changes`.
- The proposal model gains an `evaluation` field.

## v5.0 — Integration and release readiness

### Carry-over fix
Assistant date-times without a time zone are now read as UTC. Mixing naive and zone-aware due dates made task listing fail; the v4.7 review found this.

### Release checks
- **Migrations:** they are append-only and uniquely numbered, and 0060–0066 drop, truncate or delete nothing.
- **Defaults:** the execution-disabled defaults hold. Tools, chat tools, browser, the scheduler runner and chat workspace context are all off by default.
- **Fresh install (live):** every local migration is applied, and `verify` passes.
- **Upgrade from the v4.0 schema (live).** The test starts from migrations 0001–0059 with existing approval and knowledge rows, then installs the full set:
  - it applies 0060–0066 only;
  - the existing rows survive, picking up the new column defaults;
  - `verify` passes;
  - a second install changes nothing.

### Combined regression
All of these ran on a clean checkout of `72af343`:

| Suite | Result |
|---|---|
| Python unit and API | 1230 passed, 0 failed, 17 skipped (symlink tests need Windows privileges) |
| Live PostgreSQL 18.6 | 67 passed, 0 failed, 0 skipped |
| Frontend Vitest | 132 passed |
| TypeScript and production build | pass |
| Playwright E2E | 11 passed |
| Live Uvicorn smoke | approval flow: 202, then 409 before approval, then 200 with exactly one write, then 409 on reuse; token absent from the log |

### Verdict: READY to merge into main
Every version v4.1–v5.0 was accepted after an independent review, with every finding fixed or explicitly documented. All suites pass on the release commit. Database changes are forward-only.

You merge the pull request yourself. Three decisions are still yours:
1. **PDF parsing.** It needs a PDF library, which is a new dependency. Until you approve one, PDF uploads are refused.
2. **Real-model check.** Put your API key and exact model ID in the git-ignored `.env`, then run a plain chat and a tool-assisted chat.
3. **Installing on `jarvis_AI`.** The working database has not been touched. The new migrations should be installed only with your consent, after a backup:
   1. `python -m database.runner backup`
   2. `install`
   3. `verify`

### Known limits carried forward
- **Locks are per process:** the integration revoke/action lock and the file-backed prompt writes.
- **A timed-out workflow step keeps running:** its thread cannot be stopped, so its outcome is reported as unknown.
- **Scheduler delivery is at most once.**
- **Improvement evaluation is offline:** static checks only; no model is run.
- **The only device adapter is simulated.**
- **Chat uses the configured provider,** not the v5.1 model router.

