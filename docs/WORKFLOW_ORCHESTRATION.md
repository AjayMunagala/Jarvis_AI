# Advanced Planning & Workflow Orchestration (v3.2)

v3.2 introduces DAG-based multi-step workflow orchestration, topological execution levels, failure containment, dependency skipping, and execution audit trails.

## Core Architecture

1. **DAG Graph Validation & Cycle Detection**:
   - Workflows are defined as Directed Acyclic Graphs (`DAGSpec`) consisting of `WorkflowStepDefinition` items.
   - Self-referencing steps, missing upstream dependencies, and cycles (both direct and multi-step indirect) are rejected at model validation time using Kahn's topological sort algorithm.
   - Steps are partitioned into parallel topological levels (`topological_levels()`) where steps within the same level can execute concurrently.

2. **Step Lifecycle & Failure Containment**:
   - Each step transitions through: `PENDING` -> `RUNNING` -> `COMPLETED` | `FAILED` | `SKIPPED`.
   - If an upstream step fails or is skipped, all downstream steps depending on it are automatically marked `SKIPPED` with an explanatory diagnostic message.
   - Independent branches of the DAG continue executing without disruption.

3. **Persistent Recovery Ready (v3.8 Forward-Compatibility)**:
   - `0008_workflow_engine.sql` provisions `workflow_step_runs` with `recovery_metadata JSONB NOT NULL DEFAULT '{}'::jsonb`.
   - This ensures full persistence for step compensation, retry attempts, and checkpoint state in cross-module workflows without introducing duplicate migrations.

4. **Security & Capabilities**:
   - `Capability.WORKFLOW_READ` controls reading definitions and inspecting run progress.
   - `Capability.WORKFLOW_WRITE` controls creating/deleting definitions, triggering runs, and cancelling runs.
   - Strictly single-owner isolated.

---

## Database Schema (Migration `0008_workflow_engine.sql`)

- `jarvis_app.workflow_definitions`: ID, owner, name, description, `dag_spec JSONB`, timestamps.
- `jarvis_app.workflow_runs`: ID, workflow_id, owner, status (`pending`, `running`, `completed`, `failed`, `cancelled`), inputs, outputs, error_message, timestamps.
- `jarvis_app.workflow_step_runs`: ID, run_id, step_id, step_name, status (`pending`, `running`, `completed`, `failed`, `skipped`), inputs, outputs, error_message, `recovery_metadata JSONB`, timestamps.

---

## API Endpoints

All endpoints reside under `/workflows`:

| Endpoint | Method | Capability | Description |
|---|---|---|---|
| `/workflows/definitions` | `POST` | `WORKFLOW_WRITE` | Create DAG definition (validates acyclicity) |
| `/workflows/definitions` | `GET` | `WORKFLOW_READ` | List definitions with pagination |
| `/workflows/definitions/{id}` | `GET` | `WORKFLOW_READ` | Get workflow definition |
| `/workflows/definitions/{id}` | `DELETE` | `WORKFLOW_WRITE` | Delete workflow definition |
| `/workflows/definitions/{id}/run` | `POST` | `WORKFLOW_WRITE` | Trigger DAG workflow run |
| `/workflows/runs` | `GET` | `WORKFLOW_READ` | List workflow runs (status/workflow filter) |
| `/workflows/runs/{run_id}` | `GET` | `WORKFLOW_READ` | Inspect run status and step progress |
| `/workflows/runs/{run_id}/cancel` | `POST` | `WORKFLOW_WRITE` | Cancel active workflow run |

---

## Step Execution Dispatch

- `noop`: Simple pass-through.
- `transform`: Combines run inputs, prior step outputs, and step parameters.
- `assistant`: Dispatches to `AssistantService` actions (e.g. `tasks.list`, `notes.search`).
- `tool`: Invokes registered deterministic tools from `ToolRuntime`.

---

## Verification & Test Coverage

- `tests/test_workflow.py`: Unit tests for cycle detection, topological levels, `MemoryWorkflowStore`, `WorkflowService` execution, failure propagation, skipping, cancellation, and audit logging.
- `tests/test_workflow_api.py`: FastAPI TestClient integration tests for `/workflows/*`, capability enforcement (401/403), DAG schema validation (422), and CRUD lifecycles.
