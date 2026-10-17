# Cross-Module Workflows & Self-Healing (v3.8)

v3.8 introduces cross-module workflow orchestration spanning Assistant, Knowledge, Research, Scheduler, Health, and Finance subsystems, supported by a 4-tier rollback classification model, bounded exponential backoff retries, and automated compensation routines.

## Architecture & Operational Invariants

### 1. Unified Cross-Module Orchestration
- Workflow step definitions dynamically dispatch to subsystem services:
  - **Assistant**: `tasks.create`, `tasks.list`, `notes.create`, `notes.search`
  - **Finance**: `finance.accounts.create`, `finance.transactions.record`, `finance.snapshot.generate`
  - **Scheduler**: `scheduler.notifications.send`, `scheduler.jobs.create`
  - **Health**: `health.diagnostic.run`, `health.snapshot.capture`
  - **Knowledge**: `knowledge.search`, `knowledge.ingest`
  - **Research**: `research.documents.parse`, `research.synthesize`
- Prior step outputs are cleanly piped into downstream step inputs.

### 2. Four-Tier Rollback Classification
Every workflow step action is classified into an operational recovery tier:
- **Tier 1 (Idempotent)**: Read-only operations, searches, idempotent queries, and transforms. Safely skipped during rollback without side effects.
- **Tier 2 (Compensating)**: State mutations with programmatic inverses (e.g., `tasks.create` -> `tasks.delete`, `accounts.create` -> `accounts.delete`, `transactions.record` -> `transactions.delete`).
- **Tier 3 (Reversible State)**: File quarantine or snapshot alterations reversible via hash-verified restore.
- **Tier 4 (Manual Intervention Required)**: External notifications delivered to external channels that cannot be retracted. Emits a dead-letter alert and marks `compensation_status = "manual_required"`.

### 3. Bounded Retries & Exponential Backoff
- Each step is afforded up to **2 retries** (3 total attempts).
- Exponential backoff schedule: 1s, 4s (configurable / fast in test environments).
- Prevents cascading transient network or resource timeouts from halting workflows prematurely.

### 4. Reverse Compensation (LIFO) & Truthful Reporting
- Upon retry exhaustion of any step, the engine terminates further execution, marks remaining steps `SKIPPED`, and invokes the backward compensation chain over previously `COMPLETED` steps in reverse order (LIFO).
- **Truthful Status Reporting**:
  - `failed_compensated`: All previous steps successfully reversed.
  - `failed_uncompensated_manual_required`: Step had irreversible Tier 4 side effects requiring human intervention.
  - `failed_uncompensated`: A compensating action failed.
- Persisted in pre-provisioned `recovery_metadata` JSONB within `jarvis_app.workflow_step_runs` without modifying database schema.

---

## Storage & Recovery Metadata Schema

No new SQL migration is required for v3.8. Pre-provisioned in `0008_workflow_engine.sql`:
```json
{
  "compensation_status": "none | pending | compensated | failed | manual_required",
  "attempt_count": 1,
  "attempt_timestamps": ["2026-10-17T12:00:00Z"],
  "rollback_tier": "TIER_1_IDEMPOTENT | TIER_2_COMPENSATING | TIER_3_REVERSIBLE | TIER_4_MANUAL",
  "failure_reason": null,
  "compensation_log": [
    {
      "action": "delete_task",
      "timestamp": "2026-10-17T12:00:05Z",
      "status": "success",
      "details": {"deleted_task_id": "..."}
    }
  ]
}
```

---

## API Endpoints

| Endpoint | Method | Capability | Description |
|---|---|---|---|
| `/workflows/runs/{run_id}/compensate` | `POST` | `WORKFLOW_WRITE` | Trigger on-demand compensation for completed steps |
| `/workflows/recovery/dead-letters` | `GET` | `WORKFLOW_READ` | Retrieve steps requiring manual human intervention |

---

## Verification & Test Coverage

- `tests/test_cross_module_workflow.py`: Comprehensive test suite verifying tier classification, cross-module orchestration across finance and assistant, bounded retry recovery, backward LIFO compensation on crash, and Tier 4 dead-letter handling.
- `tests/test_workflow.py` & `tests/test_workflow_api.py`: Regression verification of DAG cycle detection, topological levels, execution, cancellation, and API routes.
