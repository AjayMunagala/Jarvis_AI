"""Workflow storage: in-memory (non-durable) and PostgreSQL (durable) (v3.2)."""

from datetime import datetime, timezone
import json
import threading
from time import monotonic
from typing import Any, Callable, Protocol
from uuid import UUID

from jarvis.persistence.operation_timeout import (
    bounded_connect_timeout, execute_with_deadline, operation_deadline,
)
from jarvis.workflow.models import (
    DAGSpec,
    StepStatus,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStepRun,
)


class WorkflowStorageError(Exception):
    """Sanitized storage failure for workflow operations."""


class WorkflowStore(Protocol):
    durable: bool

    def create_definition(self, definition: WorkflowDefinition) -> None: ...
    def get_definition(self, owner_id: UUID, workflow_id: UUID) -> WorkflowDefinition | None: ...
    def list_definitions(
        self, owner_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[WorkflowDefinition], int]: ...
    def delete_definition(self, owner_id: UUID, workflow_id: UUID) -> bool: ...

    def create_run(self, run: WorkflowRun, step_runs: list[WorkflowStepRun]) -> None: ...
    def get_run(self, owner_id: UUID, run_id: UUID) -> WorkflowRun | None: ...
    def get_run_steps(self, run_id: UUID) -> list[WorkflowStepRun]: ...
    def list_runs(
        self,
        owner_id: UUID,
        *,
        workflow_id: UUID | None = None,
        status: WorkflowStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[WorkflowRun], int]: ...
    def update_run(self, owner_id: UUID, run_id: UUID, updates: dict[str, Any]) -> WorkflowRun | None: ...
    def update_step_run(self, run_id: UUID, step_id: str, updates: dict[str, Any]) -> WorkflowStepRun | None: ...


# ===================================================================
# In-Memory Implementation
# ===================================================================

class MemoryWorkflowStore:
    durable: bool = False

    def __init__(self) -> None:
        self._definitions: dict[UUID, WorkflowDefinition] = {}
        self._runs: dict[UUID, WorkflowRun] = {}
        self._step_runs: dict[UUID, list[WorkflowStepRun]] = {}
        self._lock = threading.Lock()

    def create_definition(self, definition: WorkflowDefinition) -> None:
        with self._lock:
            self._definitions[definition.id] = definition

    def get_definition(self, owner_id: UUID, workflow_id: UUID) -> WorkflowDefinition | None:
        with self._lock:
            defn = self._definitions.get(workflow_id)
            return defn if defn and defn.owner_id == owner_id else None

    def list_definitions(
        self, owner_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[WorkflowDefinition], int]:
        with self._lock:
            matched = [d for d in self._definitions.values() if d.owner_id == owner_id]
            matched.sort(key=lambda x: x.created_at, reverse=True)
            return matched[offset : offset + limit], len(matched)

    def delete_definition(self, owner_id: UUID, workflow_id: UUID) -> bool:
        with self._lock:
            defn = self._definitions.get(workflow_id)
            if not defn or defn.owner_id != owner_id:
                return False
            del self._definitions[workflow_id]
            return True

    def create_run(self, run: WorkflowRun, step_runs: list[WorkflowStepRun]) -> None:
        with self._lock:
            self._runs[run.id] = run
            self._step_runs[run.id] = list(step_runs)

    def get_run(self, owner_id: UUID, run_id: UUID) -> WorkflowRun | None:
        with self._lock:
            run = self._runs.get(run_id)
            return run if run and run.owner_id == owner_id else None

    def get_run_steps(self, run_id: UUID) -> list[WorkflowStepRun]:
        with self._lock:
            return list(self._step_runs.get(run_id, []))

    def list_runs(
        self,
        owner_id: UUID,
        *,
        workflow_id: UUID | None = None,
        status: WorkflowStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[WorkflowRun], int]:
        with self._lock:
            matched = [
                r
                for r in self._runs.values()
                if r.owner_id == owner_id
                and (workflow_id is None or r.workflow_id == workflow_id)
                and (status is None or r.status == status)
            ]
            matched.sort(key=lambda x: x.created_at, reverse=True)
            return matched[offset : offset + limit], len(matched)

    def update_run(self, owner_id: UUID, run_id: UUID, updates: dict[str, Any]) -> WorkflowRun | None:
        with self._lock:
            existing = self._runs.get(run_id)
            if not existing or existing.owner_id != owner_id:
                return None
            data = existing.model_dump()
            data.update({k: v for k, v in updates.items() if v is not None})
            updated = WorkflowRun.model_validate(data)
            self._runs[run_id] = updated
            return updated

    def update_step_run(self, run_id: UUID, step_id: str, updates: dict[str, Any]) -> WorkflowStepRun | None:
        with self._lock:
            steps = self._step_runs.get(run_id)
            if not steps:
                return None
            for idx, step in enumerate(steps):
                if step.step_id == step_id:
                    data = step.model_dump()
                    data.update({k: v for k, v in updates.items() if v is not None})
                    updated = WorkflowStepRun.model_validate(data)
                    steps[idx] = updated
                    return updated
            return None


# ===================================================================
# PostgreSQL Implementation
# ===================================================================

class PostgresWorkflowStore:
    durable: bool = True

    def __init__(
        self,
        database_url: str,
        *,
        connect_timeout_seconds: int = 3,
        operation_timeout_seconds: int = 5,
        connect: Callable | None = None,
    ) -> None:
        if connect is None:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError("PostgreSQL storage requires psycopg.") from exc
            connect = psycopg.connect
        self._database_url = database_url
        self._connect = connect
        self._connect_timeout = connect_timeout_seconds
        self._operation_timeout = operation_timeout_seconds

    def _open(self, deadline: float):
        timeout = bounded_connect_timeout(self._connect_timeout, deadline, clock=monotonic)
        return self._connect(self._database_url, connect_timeout=timeout)

    def create_definition(self, definition: WorkflowDefinition) -> None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        dag_json = definition.dag_spec.model_dump_json()
        sql = """
            INSERT INTO jarvis_app.workflow_definitions (id, owner_id, name, description, dag_spec, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        try:
            with self._open(deadline) as conn:
                execute_with_deadline(
                    conn, deadline, sql,
                    (definition.id, definition.owner_id, definition.name, definition.description, dag_json, definition.created_at, definition.updated_at),
                    clock=monotonic,
                )
        except Exception as exc:
            raise WorkflowStorageError("Failed to create workflow definition in PostgreSQL.") from exc

    def get_definition(self, owner_id: UUID, workflow_id: UUID) -> WorkflowDefinition | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = """
            SELECT id, owner_id, name, description, dag_spec, created_at, updated_at
            FROM jarvis_app.workflow_definitions
            WHERE id = %s AND owner_id = %s
        """
        try:
            with self._open(deadline) as conn:
                row = execute_with_deadline(conn, deadline, sql, (workflow_id, owner_id), clock=monotonic).fetchone()
                if not row:
                    return None
                return WorkflowDefinition(
                    id=row[0],
                    owner_id=row[1],
                    name=row[2],
                    description=row[3],
                    dag_spec=DAGSpec.model_validate(json.loads(row[4]) if isinstance(row[4], str) else row[4]),
                    created_at=row[5],
                    updated_at=row[6],
                )
        except Exception as exc:
            raise WorkflowStorageError("Failed to get workflow definition from PostgreSQL.") from exc

    def list_definitions(
        self, owner_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[WorkflowDefinition], int]:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        count_sql = "SELECT COUNT(*) FROM jarvis_app.workflow_definitions WHERE owner_id = %s"
        data_sql = """
            SELECT id, owner_id, name, description, dag_spec, created_at, updated_at
            FROM jarvis_app.workflow_definitions
            WHERE owner_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """
        try:
            with self._open(deadline) as conn:
                total = execute_with_deadline(conn, deadline, count_sql, (owner_id,), clock=monotonic).fetchone()[0]
                rows = execute_with_deadline(conn, deadline, data_sql, (owner_id, limit, offset), clock=monotonic).fetchall()
                items = [
                    WorkflowDefinition(
                        id=r[0],
                        owner_id=r[1],
                        name=r[2],
                        description=r[3],
                        dag_spec=DAGSpec.model_validate(json.loads(r[4]) if isinstance(r[4], str) else r[4]),
                        created_at=r[5],
                        updated_at=r[6],
                    )
                    for r in rows
                ]
                return items, total
        except Exception as exc:
            raise WorkflowStorageError("Failed to list workflow definitions from PostgreSQL.") from exc

    def delete_definition(self, owner_id: UUID, workflow_id: UUID) -> bool:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = "DELETE FROM jarvis_app.workflow_definitions WHERE id = %s AND owner_id = %s"
        try:
            with self._open(deadline) as conn:
                res = execute_with_deadline(conn, deadline, sql, (workflow_id, owner_id), clock=monotonic)
                return getattr(res, "rowcount", 1) > 0
        except Exception as exc:
            raise WorkflowStorageError("Failed to delete workflow definition from PostgreSQL.") from exc

    def create_run(self, run: WorkflowRun, step_runs: list[WorkflowStepRun]) -> None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        run_sql = """
            INSERT INTO jarvis_app.workflow_runs (id, workflow_id, owner_id, status, inputs, outputs, error_message, created_at, started_at, completed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        step_sql = """
            INSERT INTO jarvis_app.workflow_step_runs (id, run_id, step_id, step_name, status, inputs, outputs, error_message, recovery_metadata, started_at, completed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        try:
            with self._open(deadline) as conn:
                execute_with_deadline(
                    conn, deadline, run_sql,
                    (run.id, run.workflow_id, run.owner_id, run.status.value,
                     json.dumps(run.inputs), json.dumps(run.outputs), run.error_message,
                     run.created_at, run.started_at, run.completed_at),
                    clock=monotonic,
                )
                for step in step_runs:
                    execute_with_deadline(
                        conn, deadline, step_sql,
                        (step.id, step.run_id, step.step_id, step.step_name, step.status.value,
                         json.dumps(step.inputs), json.dumps(step.outputs), step.error_message,
                         json.dumps(step.recovery_metadata), step.started_at, step.completed_at),
                        clock=monotonic,
                    )
        except Exception as exc:
            raise WorkflowStorageError("Failed to create workflow run in PostgreSQL.") from exc

    def get_run(self, owner_id: UUID, run_id: UUID) -> WorkflowRun | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = """
            SELECT id, workflow_id, owner_id, status, inputs, outputs, error_message, created_at, started_at, completed_at
            FROM jarvis_app.workflow_runs
            WHERE id = %s AND owner_id = %s
        """
        try:
            with self._open(deadline) as conn:
                row = execute_with_deadline(conn, deadline, sql, (run_id, owner_id), clock=monotonic).fetchone()
                if not row:
                    return None
                return WorkflowRun(
                    id=row[0],
                    workflow_id=row[1],
                    owner_id=row[2],
                    status=WorkflowStatus(row[3]),
                    inputs=json.loads(row[4]) if isinstance(row[4], str) else row[4],
                    outputs=json.loads(row[5]) if isinstance(row[5], str) else row[5],
                    error_message=row[6],
                    created_at=row[7],
                    started_at=row[8],
                    completed_at=row[9],
                )
        except Exception as exc:
            raise WorkflowStorageError("Failed to get workflow run from PostgreSQL.") from exc

    def get_run_steps(self, run_id: UUID) -> list[WorkflowStepRun]:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = """
            SELECT id, run_id, step_id, step_name, status, inputs, outputs, error_message, recovery_metadata, started_at, completed_at
            FROM jarvis_app.workflow_step_runs
            WHERE run_id = %s
            ORDER BY started_at ASC NULLS LAST, id ASC
        """
        try:
            with self._open(deadline) as conn:
                rows = execute_with_deadline(conn, deadline, sql, (run_id,), clock=monotonic).fetchall()
                return [
                    WorkflowStepRun(
                        id=r[0],
                        run_id=r[1],
                        step_id=r[2],
                        step_name=r[3],
                        status=StepStatus(r[4]),
                        inputs=json.loads(r[5]) if isinstance(r[5], str) else r[5],
                        outputs=json.loads(r[6]) if isinstance(r[6], str) else r[6],
                        error_message=r[7],
                        recovery_metadata=json.loads(r[8]) if isinstance(r[8], str) else r[8],
                        started_at=r[9],
                        completed_at=r[10],
                    )
                    for r in rows
                ]
        except Exception as exc:
            raise WorkflowStorageError("Failed to get workflow step runs from PostgreSQL.") from exc

    def list_runs(
        self,
        owner_id: UUID,
        *,
        workflow_id: UUID | None = None,
        status: WorkflowStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[WorkflowRun], int]:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        clauses = ["owner_id = %s"]
        params: list[Any] = [owner_id]
        if workflow_id:
            clauses.append("workflow_id = %s")
            params.append(workflow_id)
        if status:
            clauses.append("status = %s")
            params.append(status.value)
        where = " AND ".join(clauses)
        count_sql = f"SELECT COUNT(*) FROM jarvis_app.workflow_runs WHERE {where}"
        data_sql = f"""
            SELECT id, workflow_id, owner_id, status, inputs, outputs, error_message, created_at, started_at, completed_at
            FROM jarvis_app.workflow_runs
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """
        try:
            with self._open(deadline) as conn:
                total = execute_with_deadline(conn, deadline, count_sql, tuple(params), clock=monotonic).fetchone()[0]
                rows = execute_with_deadline(conn, deadline, data_sql, tuple(params + [limit, offset]), clock=monotonic).fetchall()
                items = [
                    WorkflowRun(
                        id=r[0],
                        workflow_id=r[1],
                        owner_id=r[2],
                        status=WorkflowStatus(r[3]),
                        inputs=json.loads(r[4]) if isinstance(r[4], str) else r[4],
                        outputs=json.loads(r[5]) if isinstance(r[5], str) else r[5],
                        error_message=r[6],
                        created_at=r[7],
                        started_at=r[8],
                        completed_at=r[9],
                    )
                    for r in rows
                ]
                return items, total
        except Exception as exc:
            raise WorkflowStorageError("Failed to list workflow runs from PostgreSQL.") from exc

    def update_run(self, owner_id: UUID, run_id: UUID, updates: dict[str, Any]) -> WorkflowRun | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        existing = self.get_run(owner_id, run_id)
        if not existing:
            return None
        data = existing.model_dump()
        data.update({k: v for k, v in updates.items() if v is not None})
        updated = WorkflowRun.model_validate(data)
        sql = """
            UPDATE jarvis_app.workflow_runs
            SET status = %s, outputs = %s, error_message = %s, started_at = %s, completed_at = %s
            WHERE id = %s AND owner_id = %s
        """
        try:
            with self._open(deadline) as conn:
                execute_with_deadline(
                    conn, deadline, sql,
                    (updated.status.value, json.dumps(updated.outputs), updated.error_message,
                     updated.started_at, updated.completed_at, run_id, owner_id),
                    clock=monotonic,
                )
                return updated
        except Exception as exc:
            raise WorkflowStorageError("Failed to update workflow run in PostgreSQL.") from exc

    def update_step_run(self, run_id: UUID, step_id: str, updates: dict[str, Any]) -> WorkflowStepRun | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = """
            UPDATE jarvis_app.workflow_step_runs
            SET status = COALESCE(%s, status),
                outputs = COALESCE(%s, outputs),
                error_message = COALESCE(%s, error_message),
                recovery_metadata = COALESCE(%s, recovery_metadata),
                started_at = COALESCE(%s, started_at),
                completed_at = COALESCE(%s, completed_at)
            WHERE run_id = %s AND step_id = %s
        """
        status_val = updates["status"].value if "status" in updates and updates["status"] else None
        outputs_val = json.dumps(updates["outputs"]) if "outputs" in updates and updates["outputs"] is not None else None
        err_val = updates.get("error_message")
        rec_val = json.dumps(updates["recovery_metadata"]) if "recovery_metadata" in updates and updates["recovery_metadata"] is not None else None
        started_val = updates.get("started_at")
        completed_val = updates.get("completed_at")
        try:
            with self._open(deadline) as conn:
                execute_with_deadline(
                    conn, deadline, sql,
                    (status_val, outputs_val, err_val, rec_val, started_val, completed_val, run_id, step_id),
                    clock=monotonic,
                )
            for s in self.get_run_steps(run_id):
                if s.step_id == step_id:
                    return s
            return None
        except Exception as exc:
            raise WorkflowStorageError("Failed to update workflow step run in PostgreSQL.") from exc
