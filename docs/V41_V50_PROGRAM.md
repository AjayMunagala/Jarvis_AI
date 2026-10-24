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
| v4.6–v5.0 | NOT STARTED | — | — |

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

