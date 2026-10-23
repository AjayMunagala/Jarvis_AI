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
| v4.1 | IMPLEMENTED, TESTED; awaiting owner acceptance | See below | recorded in the acceptance report |
| v4.2–v5.0 | NOT STARTED | — | — |

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
- Quarantine and self-improvement approvals are still self-approval by the same owner (v4.3).
- `/autonomous/execute` still defaults to `dry_run=False` (v4.3).
- These defects remain open:
  - finance balance updates are not atomic;
  - `compensate` is not idempotent (v4.4);
  - the scheduler has no background runner (v4.5);
  - `hash()` embeddings (v4.6).
- Fake CPU and memory values and the always-healthy scheduler `health_check` remain until v4.2.
