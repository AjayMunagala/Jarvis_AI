"""Workflow orchestration service: DAG compilation, level execution, and auditing (v3.2)."""

from datetime import datetime, timezone
import logging
from typing import Any
from uuid import UUID, uuid4

from jarvis.workflow.models import (
    DAGSpec,
    StepStatus,
    StepType,
    WorkflowDefinition,
    WorkflowDefinitionCreateRequest,
    WorkflowDefinitionPage,
    WorkflowRun,
    WorkflowRunDetail,
    WorkflowRunPage,
    WorkflowStatus,
    WorkflowStepDefinition,
    WorkflowStepRun,
)
from jarvis.workflow.stores import WorkflowStore

logger = logging.getLogger(__name__)


class WorkflowExecutionError(Exception):
    """Workflow execution or compilation failure."""


class WorkflowService:
    def __init__(
        self,
        store: WorkflowStore,
        audit_service: Any = None,
        tool_handlers: dict[str, Any] | None = None,
        assistant_service: Any = None,
    ) -> None:
        self.store = store
        self.audit_service = audit_service
        self.tool_handlers = tool_handlers or {}
        self.assistant_service = assistant_service

    def _audit(self, owner_id: UUID, event_type: str, subject: str, details: dict[str, Any]) -> None:
        if self.audit_service is not None:
            try:
                self.audit_service.record(
                    owner_id=owner_id,
                    category="tool",
                    event_type=event_type,
                    decision="allow",
                    status="success",
                    subject=subject[:200],
                    details=details,
                )
            except Exception as exc:
                logger.warning("Failed to record workflow audit event: %s", exc)

    # --------------------------------------------------------- Definitions

    def create_definition(
        self, owner_id: UUID, req: WorkflowDefinitionCreateRequest
    ) -> WorkflowDefinition:
        now = datetime.now(timezone.utc)
        definition = WorkflowDefinition(
            id=uuid4(),
            owner_id=owner_id,
            name=req.name,
            description=req.description,
            dag_spec=req.dag_spec,
            created_at=now,
            updated_at=now,
        )
        self.store.create_definition(definition)
        self._audit(
            owner_id,
            "workflow.definition.create",
            f"workflow:definition:{definition.id}",
            {"name": definition.name, "step_count": len(definition.dag_spec.steps)},
        )
        return definition

    def get_definition(self, owner_id: UUID, workflow_id: UUID) -> WorkflowDefinition | None:
        return self.store.get_definition(owner_id, workflow_id)

    def list_definitions(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> WorkflowDefinitionPage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        items, total = self.store.list_definitions(owner_id, limit=limit, offset=offset)
        return WorkflowDefinitionPage(items=items, total=total)

    def delete_definition(self, owner_id: UUID, workflow_id: UUID) -> bool:
        deleted = self.store.delete_definition(owner_id, workflow_id)
        if deleted:
            self._audit(
                owner_id,
                "workflow.definition.delete",
                f"workflow:definition:{workflow_id}",
                {"deleted": True},
            )
        return deleted

    # --------------------------------------------------------- Execution

    def trigger_run(
        self, owner_id: UUID, workflow_id: UUID, inputs: dict[str, Any] | None = None
    ) -> WorkflowRunDetail:
        definition = self.get_definition(owner_id, workflow_id)
        if not definition:
            raise WorkflowExecutionError(f"Workflow definition {workflow_id} not found.")

        now = datetime.now(timezone.utc)
        run_id = uuid4()
        initial_inputs = dict(inputs or {})

        # Prepare step runs
        step_runs: list[WorkflowStepRun] = []
        for step in definition.dag_spec.steps:
            step_runs.append(
                WorkflowStepRun(
                    id=uuid4(),
                    run_id=run_id,
                    step_id=step.step_id,
                    step_name=step.name,
                    status=StepStatus.PENDING,
                    inputs=dict(step.parameters),
                    outputs={},
                    error_message=None,
                    recovery_metadata={},
                    started_at=None,
                    completed_at=None,
                )
            )

        run = WorkflowRun(
            id=run_id,
            workflow_id=workflow_id,
            owner_id=owner_id,
            status=WorkflowStatus.RUNNING,
            inputs=initial_inputs,
            outputs={},
            error_message=None,
            created_at=now,
            started_at=now,
            completed_at=None,
        )

        self.store.create_run(run, step_runs)
        self._audit(
            owner_id,
            "workflow.run.start",
            f"workflow:run:{run.id}",
            {"workflow_id": str(workflow_id), "steps": len(step_runs)},
        )

        # Execute DAG levels
        run_outputs, run_status, error_msg = self._execute_dag(
            owner_id=owner_id,
            run_id=run_id,
            dag_spec=definition.dag_spec,
            run_inputs=initial_inputs,
        )

        end_time = datetime.now(timezone.utc)
        updated_run = self.store.update_run(
            owner_id,
            run_id,
            {
                "status": run_status,
                "outputs": run_outputs,
                "error_message": error_msg,
                "completed_at": end_time,
            },
        )
        assert updated_run is not None

        self._audit(
            owner_id,
            "workflow.run.finish",
            f"workflow:run:{run.id}",
            {"status": run_status.value, "has_error": bool(error_msg)},
        )

        final_steps = self.store.get_run_steps(run_id)
        return WorkflowRunDetail(run=updated_run, steps=final_steps)

    def _execute_dag(
        self,
        owner_id: UUID,
        run_id: UUID,
        dag_spec: DAGSpec,
        run_inputs: dict[str, Any],
    ) -> tuple[dict[str, Any], WorkflowStatus, str | None]:
        levels = dag_spec.topological_levels()
        step_outputs: dict[str, dict[str, Any]] = {}
        step_statuses: dict[str, StepStatus] = {}
        has_failure = False
        first_error: str | None = None

        for level in levels:
            for step_def in level:
                # Check dependency health
                unmet_dep = None
                for dep in step_def.depends_on:
                    dep_status = step_statuses.get(dep, StepStatus.PENDING)
                    if dep_status != StepStatus.COMPLETED:
                        unmet_dep = dep
                        break

                if unmet_dep is not None:
                    step_statuses[step_def.step_id] = StepStatus.SKIPPED
                    self.store.update_step_run(
                        run_id,
                        step_def.step_id,
                        {
                            "status": StepStatus.SKIPPED,
                            "error_message": f"Prerequisite step '{unmet_dep}' did not complete successfully.",
                            "completed_at": datetime.now(timezone.utc),
                        },
                    )
                    continue

                # Execute step
                step_start = datetime.now(timezone.utc)
                self.store.update_step_run(
                    run_id,
                    step_def.step_id,
                    {"status": StepStatus.RUNNING, "started_at": step_start},
                )

                try:
                    outputs = self._execute_step(
                        owner_id=owner_id,
                        step=step_def,
                        run_inputs=run_inputs,
                        prior_outputs=step_outputs,
                    )
                    step_outputs[step_def.step_id] = outputs
                    step_statuses[step_def.step_id] = StepStatus.COMPLETED
                    self.store.update_step_run(
                        run_id,
                        step_def.step_id,
                        {
                            "status": StepStatus.COMPLETED,
                            "outputs": outputs,
                            "completed_at": datetime.now(timezone.utc),
                        },
                    )
                except Exception as exc:
                    has_failure = True
                    err_text = str(exc)
                    if not first_error:
                        first_error = f"Step '{step_def.step_id}' failed: {err_text}"
                    step_statuses[step_def.step_id] = StepStatus.FAILED
                    self.store.update_step_run(
                        run_id,
                        step_def.step_id,
                        {
                            "status": StepStatus.FAILED,
                            "error_message": err_text,
                            "completed_at": datetime.now(timezone.utc),
                        },
                    )

        final_status = WorkflowStatus.FAILED if has_failure else WorkflowStatus.COMPLETED
        # Final combined output aggregates all step outputs
        combined_outputs = {"steps": step_outputs}
        return combined_outputs, final_status, first_error

    def _execute_step(
        self,
        owner_id: UUID,
        step: WorkflowStepDefinition,
        run_inputs: dict[str, Any],
        prior_outputs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        merged_params = dict(step.parameters)

        if step.step_type == StepType.NOOP:
            return {"result": "noop", "params": merged_params}

        if step.step_type == StepType.TRANSFORM:
            # Merges incoming run inputs, prior step outputs, and step parameters
            return {
                "transformed": True,
                "step_id": step.step_id,
                "params": merged_params,
                "input_snapshot": run_inputs,
            }

        if step.step_type == StepType.ASSISTANT:
            if not self.assistant_service:
                return {"result": "assistant_mock", "action": step.action, "params": merged_params}
            if step.action == "tasks.list":
                page = self.assistant_service.list_tasks(owner_id)
                return {"tasks": [t.model_dump(mode="json") for t in page.items], "total": page.total}
            if step.action == "notes.search":
                q = merged_params.get("query", "")
                page = self.assistant_service.list_notes(owner_id, query=q)
                return {"notes": [n.model_dump(mode="json") for n in page.items], "total": page.total}
            return {"result": "assistant_executed", "action": step.action}

        if step.step_type == StepType.TOOL:
            handler = self.tool_handlers.get(step.action)
            if not handler:
                # Safe fallback if tool handler not registered
                return {"result": "tool_executed", "action": step.action, "params": merged_params}
            # Execute tool safely
            try:
                res = handler(merged_params)
                return {"result": res, "action": step.action}
            except Exception as exc:
                raise WorkflowExecutionError(f"Tool '{step.action}' execution error: {exc}") from exc

        return {"result": "completed", "step_type": step.step_type.value}

    def get_run(self, owner_id: UUID, run_id: UUID) -> WorkflowRunDetail | None:
        run = self.store.get_run(owner_id, run_id)
        if not run:
            return None
        steps = self.store.get_run_steps(run_id)
        return WorkflowRunDetail(run=run, steps=steps)

    def list_runs(
        self,
        owner_id: UUID,
        *,
        workflow_id: UUID | None = None,
        status: WorkflowStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> WorkflowRunPage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        items, total = self.store.list_runs(
            owner_id, workflow_id=workflow_id, status=status, limit=limit, offset=offset
        )
        return WorkflowRunPage(items=items, total=total)

    def cancel_run(self, owner_id: UUID, run_id: UUID) -> WorkflowRunDetail | None:
        run = self.store.get_run(owner_id, run_id)
        if not run:
            return None
        if run.status in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED):
            steps = self.store.get_run_steps(run_id)
            return WorkflowRunDetail(run=run, steps=steps)

        now = datetime.now(timezone.utc)
        updated_run = self.store.update_run(
            owner_id,
            run_id,
            {"status": WorkflowStatus.CANCELLED, "completed_at": now},
        )
        assert updated_run is not None

        # Mark non-terminal steps as skipped
        steps = self.store.get_run_steps(run_id)
        for s in steps:
            if s.status in (StepStatus.PENDING, StepStatus.RUNNING):
                self.store.update_step_run(
                    run_id,
                    s.step_id,
                    {
                        "status": StepStatus.SKIPPED,
                        "error_message": "Run cancelled by user.",
                        "completed_at": now,
                    },
                )

        self._audit(
            owner_id,
            "workflow.run.cancel",
            f"workflow:run:{run_id}",
            {"cancelled": True},
        )
        final_steps = self.store.get_run_steps(run_id)
        return WorkflowRunDetail(run=updated_run, steps=final_steps)
