"""Workflow orchestration service: DAG compilation, cross-module execution, and recovery (v3.2 & v3.8)."""

from datetime import datetime, timezone
import logging
import time
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
from jarvis.workflow.recovery import (
    BACKOFF_SCHEDULE,
    MAX_STEP_RETRIES,
    RollbackTier,
    classify_action_tier,
)
from jarvis.workflow.stores import WorkflowStore

logger = logging.getLogger(__name__)


class WorkflowExecutionError(Exception):
    """Workflow execution or compilation failure."""


class StepUnavailableError(WorkflowExecutionError):
    """v4.1: the step's capability is not configured or not implemented.

    Raised instead of returning a placeholder "success" (formerly ``*_mock``,
    ``tool_executed``, ``assistant_executed`` or a generic ``completed``).
    It is permanent, so the step fails at once without retries.
    """


class WorkflowService:
    def __init__(
        self,
        store: WorkflowStore,
        audit_service: Any = None,
        tool_handlers: dict[str, Any] | None = None,
        assistant_service: Any = None,
        knowledge_service: Any = None,
        research_service: Any = None,
        scheduler_service: Any = None,
        health_service: Any = None,
        finance_service: Any = None,
        backoff_enabled: bool = True,
        backoff_schedule: list[float] | None = None,
    ) -> None:
        self.store = store
        self.audit_service = audit_service
        self.tool_handlers = tool_handlers or {}
        self.assistant_service = assistant_service
        self.knowledge_service = knowledge_service
        self.research_service = research_service
        self.scheduler_service = scheduler_service
        self.health_service = health_service
        self.finance_service = finance_service
        self.backoff_enabled = backoff_enabled
        self.backoff_schedule = backoff_schedule or (BACKOFF_SCHEDULE if backoff_enabled else [0.001, 0.002])

    def _audit(self, owner_id: UUID, event_type: str, subject: str, details: dict[str, Any]) -> None:
        if self.audit_service is not None:
            try:
                self.audit_service.record(
                    owner_id=owner_id,
                    category="workflow",
                    event_type=event_type,
                    decision="allowed",
                    status="succeeded",
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

        # Execute DAG levels with bounded retries and compensation
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
        completed_step_defs: list[WorkflowStepDefinition] = []
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
                            "recovery_metadata": {"compensation_status": "none", "skipped_reason": f"unmet_dep:{unmet_dep}"},
                            "completed_at": datetime.now(timezone.utc),
                        },
                    )
                    continue

                if has_failure:
                    step_statuses[step_def.step_id] = StepStatus.SKIPPED
                    self.store.update_step_run(
                        run_id,
                        step_def.step_id,
                        {
                            "status": StepStatus.SKIPPED,
                            "error_message": "Prior step in workflow failed.",
                            "recovery_metadata": {"compensation_status": "none", "skipped_reason": "prior_step_failed"},
                            "completed_at": datetime.now(timezone.utc),
                        },
                    )
                    continue

                # Execute step with bounded retry
                tier = classify_action_tier(step_def.step_type.value, step_def.action)
                attempt = 0
                attempt_timestamps: list[str] = []
                step_success = False
                outputs: dict[str, Any] = {}
                err_text: str | None = None

                while attempt <= MAX_STEP_RETRIES:
                    attempt += 1
                    ts = datetime.now(timezone.utc)
                    attempt_timestamps.append(ts.isoformat())

                    self.store.update_step_run(
                        run_id,
                        step_def.step_id,
                        {"status": StepStatus.RUNNING, "started_at": ts},
                    )

                    try:
                        outputs = self._execute_step(
                            owner_id=owner_id,
                            step=step_def,
                            run_inputs=run_inputs,
                            prior_outputs=step_outputs,
                        )
                        step_success = True
                        break
                    except StepUnavailableError as exc:
                        err_text = str(exc)
                        break  # permanent: retrying cannot make a missing capability appear
                    except Exception as exc:
                        err_text = str(exc)
                        logger.warning(
                            "Step '%s' attempt %d/%d failed: %s",
                            step_def.step_id,
                            attempt,
                            MAX_STEP_RETRIES + 1,
                            err_text,
                        )
                        if attempt <= MAX_STEP_RETRIES and self.backoff_enabled:
                            delay = self.backoff_schedule[min(attempt - 1, len(self.backoff_schedule) - 1)]
                            time.sleep(delay)

                if step_success:
                    step_outputs[step_def.step_id] = outputs
                    step_statuses[step_def.step_id] = StepStatus.COMPLETED
                    completed_step_defs.append(step_def)
                    self.store.update_step_run(
                        run_id,
                        step_def.step_id,
                        {
                            "status": StepStatus.COMPLETED,
                            "outputs": outputs,
                            "recovery_metadata": {
                                "compensation_status": "none",
                                "attempt_count": attempt,
                                "attempt_timestamps": attempt_timestamps,
                                "rollback_tier": tier.value,
                                "failure_reason": None,
                                "compensation_log": [],
                            },
                            "completed_at": datetime.now(timezone.utc),
                        },
                    )
                else:
                    has_failure = True
                    step_statuses[step_def.step_id] = StepStatus.FAILED
                    if not first_error:
                        first_error = f"Step '{step_def.step_id}' failed: {err_text}"

                    self.store.update_step_run(
                        run_id,
                        step_def.step_id,
                        {
                            "status": StepStatus.FAILED,
                            "error_message": err_text,
                            "recovery_metadata": {
                                "compensation_status": "none",
                                "attempt_count": attempt,
                                "attempt_timestamps": attempt_timestamps,
                                "rollback_tier": tier.value,
                                "failure_reason": err_text,
                                "compensation_log": [],
                            },
                            "completed_at": datetime.now(timezone.utc),
                        },
                    )

        # If failed, execute reverse (LIFO) compensation chain
        compensation_summary: list[dict[str, Any]] = []
        overall_compensation = "none"

        if has_failure and completed_step_defs:
            overall_compensation = "failed_compensated"
            for completed_def in reversed(completed_step_defs):
                step_out = step_outputs.get(completed_def.step_id, {})
                c_status, c_log = self._compensate_step(owner_id, completed_def, step_out)

                if c_status == "manual_required":
                    overall_compensation = "failed_uncompensated_manual_required"
                elif c_status == "failed" and overall_compensation != "failed_uncompensated_manual_required":
                    overall_compensation = "failed_uncompensated"

                # Update step run's recovery_metadata
                run_steps = self.store.get_run_steps(run_id)
                prior_meta = next(
                    (dict(s.recovery_metadata) for s in run_steps if s.step_id == completed_def.step_id),
                    {},
                )
                prior_meta["compensation_status"] = c_status
                prior_meta["compensation_log"] = c_log
                self.store.update_step_run(
                    run_id,
                    completed_def.step_id,
                    {"recovery_metadata": prior_meta},
                )
                compensation_summary.append({
                    "step_id": completed_def.step_id,
                    "status": c_status,
                    "log": c_log,
                })

        final_status = WorkflowStatus.FAILED if has_failure else WorkflowStatus.COMPLETED
        combined_outputs = {
            "steps": step_outputs,
            "recovery": {
                "verdict": overall_compensation,
                "compensated_steps": compensation_summary,
            },
        }
        final_err = f"{first_error} (Recovery: {overall_compensation})" if has_failure else None
        return combined_outputs, final_status, final_err

    def _execute_step(
        self,
        owner_id: UUID,
        step: WorkflowStepDefinition,
        run_inputs: dict[str, Any],
        prior_outputs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        merged_params = dict(step.parameters)
        act = step.action.strip().lower()

        if step.step_type == StepType.NOOP:
            return {"result": "noop", "params": merged_params}

        if step.step_type == StepType.TRANSFORM:
            return {
                "transformed": True,
                "step_id": step.step_id,
                "params": merged_params,
                "input_snapshot": run_inputs,
            }

        # ------------------- ASSISTANT
        if step.step_type == StepType.ASSISTANT or act.startswith("assistant.") or act.startswith("tasks.") or act.startswith("notes."):
            if not self.assistant_service:
                raise StepUnavailableError(f"assistant service is not configured; step '{step.action}' was not executed.")
            if act in ("tasks.list", "assistant.tasks.list"):
                page = self.assistant_service.list_tasks(owner_id)
                return {"tasks": [t.model_dump(mode="json") for t in page.items], "total": page.total}
            if act in ("tasks.create", "assistant.tasks.create"):
                from jarvis.assistant.models import TaskCreateRequest
                task = self.assistant_service.create_task(
                    owner_id,
                    TaskCreateRequest(
                        title=merged_params.get("title", f"Workflow Task {step.step_id}"),
                        description=merged_params.get("description"),
                        priority=merged_params.get("priority", "medium"),
                    ),
                )
                return {"task_id": str(task.id), "title": task.title}
            if act in ("notes.search", "assistant.notes.search"):
                q = merged_params.get("query", "")
                page = self.assistant_service.list_notes(owner_id, query=q)
                return {"notes": [n.model_dump(mode="json") for n in page.items], "total": page.total}
            if act in ("notes.create", "assistant.notes.create"):
                from jarvis.assistant.models import NoteCreateRequest
                note = self.assistant_service.create_note(
                    owner_id,
                    NoteCreateRequest(
                        title=merged_params.get("title", f"Workflow Note {step.step_id}"),
                        content=merged_params.get("content", ""),
                    ),
                )
                return {"note_id": str(note.id), "title": note.title}
            raise StepUnavailableError(f"Unsupported assistant action '{step.action}'; nothing was executed.")

        # ------------------- FINANCE
        if act.startswith("finance.") or act.startswith("accounts."):
            if not self.finance_service:
                raise StepUnavailableError(f"finance service is not configured; step '{step.action}' was not executed.")
            if act in ("finance.accounts.create", "accounts.create"):
                from jarvis.finance.models import AccountCreateRequest
                acc = self.finance_service.create_account(
                    owner_id,
                    AccountCreateRequest(
                        name=merged_params.get("name", "Workflow Account"),
                        account_type=merged_params.get("account_type", "checking"),
                        institution=merged_params.get("institution", "Internal"),
                        balance=float(merged_params.get("balance", 0.0)),
                    ),
                )
                return {"account_id": str(acc.id), "name": acc.name, "balance": acc.balance}
            if act in ("finance.transactions.record", "transactions.record"):
                from jarvis.finance.models import TransactionCreateRequest
                tx = self.finance_service.record_transaction(
                    owner_id,
                    TransactionCreateRequest(
                        account_id=UUID(merged_params["account_id"]),
                        amount=float(merged_params.get("amount", 0.0)),
                        description=merged_params.get("description", "Workflow tx"),
                    ),
                )
                return {"tx_id": str(tx.id), "amount": tx.amount, "category": tx.category}
            if act in ("finance.snapshot.generate", "finance.snapshot"):
                snap = self.finance_service.generate_snapshot(owner_id)
                return {"snapshot_id": str(snap.id), "net_worth": snap.net_worth}

        # ------------------- SCHEDULER
        if act.startswith("scheduler.") or act.startswith("notifications."):
            if not self.scheduler_service:
                raise StepUnavailableError(f"scheduler service is not configured; step '{step.action}' was not executed.")
            if act in ("scheduler.notifications.send", "notifications.send"):
                from jarvis.scheduler.models import NotificationCreateRequest
                notif = self.scheduler_service.dispatch_notification(
                    owner_id,
                    NotificationCreateRequest(
                        category=merged_params.get("category", "workflow"),
                        title=merged_params.get("title", f"Workflow Alert {step.step_id}"),
                        message=merged_params.get("message", "Workflow notice"),
                        priority=merged_params.get("priority", "normal"),
                    ),
                )
                return {"notification_id": str(notif.id) if notif else None}

        # ------------------- HEALTH
        if act.startswith("health."):
            if not self.health_service:
                raise StepUnavailableError(f"health service is not configured; step '{step.action}' was not executed.")
            if act in ("health.snapshot.capture", "health.snapshot"):
                snap = self.health_service.capture_snapshot(owner_id)
                return {"snapshot_id": str(snap.id), "disk_free_gb": snap.disk_free_gb}
            if act in ("health.diagnostic.run", "health.diagnostics"):
                diag = self.health_service.run_diagnostics(owner_id)
                return {"telemetry_id": str(diag.telemetry.id), "recommendations_count": len(diag.recommendations)}

        # ------------------- KNOWLEDGE
        if act.startswith("knowledge."):
            if not self.knowledge_service:
                raise StepUnavailableError(f"knowledge service is not configured; step '{step.action}' was not executed.")
            if act == "knowledge.search":
                from jarvis.knowledge.models import KnowledgeSearchRequest

                # v4.1: KnowledgeService.search takes a validated request object; the earlier keyword
                # call always raised TypeError and was retried with blocking sleeps.
                res = self.knowledge_service.search(owner_id, KnowledgeSearchRequest(
                    query=str(merged_params.get("query", "")), top_k=int(merged_params.get("top_k", 5))))
                return {"results_count": len(res.results)}

        # ------------------- RESEARCH
        if act.startswith("research."):
            if not self.research_service:
                raise StepUnavailableError(f"research service is not configured; step '{step.action}' was not executed.")
            if act == "research.documents.parse":
                from jarvis.research.models import DocumentParseRequest
                doc = self.research_service.parse_and_store_document(
                    owner_id,
                    DocumentParseRequest(
                        filename=merged_params.get("filename", "input.txt"),
                        content_type=merged_params.get("content_type", "text/plain"),
                        text_content=merged_params.get("text_content", ""),
                    ),
                )
                return {"document_id": str(doc.id), "filename": doc.filename}

        # ------------------- TOOLS
        if step.step_type == StepType.TOOL:
            handler = self.tool_handlers.get(step.action)
            if not handler:
                raise StepUnavailableError(f"No handler is registered for tool '{step.action}'; nothing was executed.")
            try:
                res = handler(merged_params)
                return {"result": res, "action": step.action}
            except Exception as exc:
                raise WorkflowExecutionError(f"Tool '{step.action}' execution error: {exc}") from exc

        raise StepUnavailableError(f"Unsupported action '{step.action}' for step type '{step.step_type.value}'; nothing was executed.")

    def _compensate_step(
        self,
        owner_id: UUID,
        step_def: WorkflowStepDefinition,
        step_output: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Executes compensating logic for a completed step upon workflow failure."""
        tier = classify_action_tier(step_def.step_type.value, step_def.action)
        act = step_def.action.strip().lower()
        now = datetime.now(timezone.utc)
        log: list[dict[str, Any]] = []

        # Tier 1: Idempotent - no compensation needed
        if tier == RollbackTier.TIER_1_IDEMPOTENT:
            log.append({
                "action": "skip",
                "timestamp": now.isoformat(),
                "status": "success",
                "details": {"reason": "Idempotent read/query requires no rollback."},
            })
            return "compensated", log

        # Tier 4: Manual intervention required (external notifications)
        if tier == RollbackTier.TIER_4_MANUAL:
            log.append({
                "action": "dead_letter_alert",
                "timestamp": now.isoformat(),
                "status": "manual_required",
                "details": {
                    "reason": f"External action '{step_def.action}' delivered irreversible side effects.",
                    "step_id": step_def.step_id,
                    "outputs": step_output,
                },
            })
            return "manual_required", log

        # Tier 2: Programmatic compensation
        try:
            if (act in ("tasks.create", "assistant.tasks.create")) and "task_id" in step_output:
                if self.assistant_service:
                    self.assistant_service.delete_task(owner_id, UUID(step_output["task_id"]))
                    log.append({
                        "action": "delete_task",
                        "timestamp": now.isoformat(),
                        "status": "success",
                        "details": {"deleted_task_id": step_output["task_id"]},
                    })
                    return "compensated", log

            if (act in ("notes.create", "assistant.notes.create")) and "note_id" in step_output:
                if self.assistant_service:
                    self.assistant_service.delete_note(owner_id, UUID(step_output["note_id"]))
                    log.append({
                        "action": "delete_note",
                        "timestamp": now.isoformat(),
                        "status": "success",
                        "details": {"deleted_note_id": step_output["note_id"]},
                    })
                    return "compensated", log

            if (act in ("finance.accounts.create", "accounts.create")) and "account_id" in step_output:
                if self.finance_service:
                    self.finance_service.delete_account(owner_id, UUID(step_output["account_id"]))
                    log.append({
                        "action": "delete_account",
                        "timestamp": now.isoformat(),
                        "status": "success",
                        "details": {"deleted_account_id": step_output["account_id"]},
                    })
                    return "compensated", log

            if (act in ("finance.transactions.record", "transactions.record")) and "tx_id" in step_output:
                if self.finance_service:
                    self.finance_service.delete_transaction(owner_id, UUID(step_output["tx_id"]))
                    log.append({
                        "action": "delete_transaction",
                        "timestamp": now.isoformat(),
                        "status": "success",
                        "details": {"deleted_tx_id": step_output["tx_id"]},
                    })
                    return "compensated", log

            # Fallback for unrecognized Tier 2 actions
            log.append({
                "action": "noop_compensate",
                "timestamp": now.isoformat(),
                "status": "success",
                "details": {"reason": f"No custom compensator required for {act}."},
            })
            return "compensated", log

        except Exception as exc:
            log.append({
                "action": "compensate_failed",
                "timestamp": now.isoformat(),
                "status": "failed",
                "details": {"error": str(exc)},
            })
            return "failed", log

    def get_dead_letters(self, owner_id: UUID) -> list[dict[str, Any]]:
        """Scans workflow runs for steps requiring manual human intervention."""
        runs_page = self.list_runs(owner_id, limit=200)
        dead_letters: list[dict[str, Any]] = []

        for run in runs_page.items:
            steps = self.store.get_run_steps(run.id)
            for step in steps:
                meta = step.recovery_metadata or {}
                if meta.get("compensation_status") == "manual_required":
                    dead_letters.append({
                        "run_id": str(run.id),
                        "step_id": step.step_id,
                        "step_name": step.step_name,
                        "failure_reason": meta.get("failure_reason"),
                        "compensation_log": meta.get("compensation_log"),
                    })
        return dead_letters

    def compensate_run(self, owner_id: UUID, run_id: UUID) -> dict[str, Any]:
        """Manually triggers compensation for a failed workflow run."""
        detail = self.get_run(owner_id, run_id)
        if not detail:
            raise WorkflowExecutionError(f"Run {run_id} not found.")

        completed_steps = [s for s in detail.steps if s.status == StepStatus.COMPLETED]
        summary: list[dict[str, Any]] = []
        overall = "compensated"

        for s in reversed(completed_steps):
            step_def = WorkflowStepDefinition(
                step_id=s.step_id,
                name=s.step_name,
                step_type=StepType.TOOL,
                action=s.inputs.get("action", s.step_name),
                parameters=s.inputs,
            )
            c_status, c_log = self._compensate_step(owner_id, step_def, s.outputs)
            if c_status != "compensated":
                overall = c_status

            meta = dict(s.recovery_metadata or {})
            meta["compensation_status"] = c_status
            meta["compensation_log"] = c_log
            self.store.update_step_run(
                run_id,
                s.step_id,
                {"recovery_metadata": meta},
            )
            summary.append({"step_id": s.step_id, "status": c_status, "log": c_log})

        return {"run_id": str(run_id), "status": overall, "summary": summary}

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
