"""Workflow orchestration service: DAG compilation, cross-module execution, and recovery (v3.2 & v3.8).

v4.4 reliable execution:
- Each step runs with a timeout and each run with an overall deadline. A
  read-only step that fails or times out may be retried; a write step runs
  once, because retrying a write that partly happened could duplicate it. A
  write step that times out is reported with an unknown outcome
  (``manual_required``), because the worker thread cannot be stopped.
- Cancel stops a live run before its next step or during a retry wait. Steps
  that already completed are kept and listed, and can be undone with
  compensate. A run left ``running`` by a restart is marked failed at startup.
- Retry waits never sleep a request thread blindly: they wait on the run's
  cancel event.
- Runs whose steps write data need an approved request (v4.3 gateway,
  category ``workflow``), bound to the definition, its steps and the inputs.
- Tool steps run through the read-only ``ToolRuntime`` and its policy.
- Compensation never reports an undo it did not do: a write without an undo
  is ``manual_required``. Compensating a run twice does nothing the second time.
"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import contextvars
from datetime import datetime, timezone
import hashlib
import json
import logging
import threading
from time import monotonic
from typing import Any, Callable
from uuid import UUID, uuid4

from jarvis.audit.models import ApprovalProposal
from jarvis.audit.service import run_approved

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


class WorkflowStateError(WorkflowExecutionError):
    """v4.4: the request conflicts with the run's state (HTTP 409)."""


# v4.4: service actions that only read. Any other service action writes; tool steps run through the read-only
# ToolRuntime. The prefixes mirror the dispatch order in ``_execute_step``.
READ_ONLY_ACTIONS = frozenset({"tasks.list", "assistant.tasks.list", "notes.search", "assistant.notes.search",
                               "knowledge.search"})
_SERVICE_PREFIXES = ("assistant.", "tasks.", "notes.", "finance.", "accounts.", "scheduler.", "notifications.",
                     "health.", "knowledge.", "research.")


def is_read_only_step(step: WorkflowStepDefinition) -> bool:
    if step.step_type in (StepType.NOOP, StepType.TRANSFORM):
        return True
    act = step.action.strip().lower()
    if act in READ_ONLY_ACTIONS:
        return True
    if step.step_type == StepType.ASSISTANT or act.startswith(_SERVICE_PREFIXES):
        return False
    return step.step_type == StepType.TOOL


class _ActiveRun:
    def __init__(self) -> None:
        self.cancel = threading.Event()
        self.done = threading.Event()


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
        tool_runtime: Any = None,
        tool_context_factory: Callable[[UUID], Any] | None = None,
        step_timeout_seconds: float = 30.0,
        run_timeout_seconds: float = 300.0,
        max_active_runs: int = 4,
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
        self.tool_runtime = tool_runtime
        self.tool_context_factory = tool_context_factory
        self.step_timeout_seconds = step_timeout_seconds
        self.run_timeout_seconds = run_timeout_seconds
        self.max_active_runs = max_active_runs
        self.approval_service: Any = None  # v4.4: runs with write steps need an approved request
        self._active: dict[UUID, _ActiveRun] = {}
        self._active_lock = threading.Lock()
        self._compensation_lock = threading.Lock()
        self.recovery_done = threading.Event()
        self._step_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jarvis-workflow-step")
        self._run_pool = ThreadPoolExecutor(max_workers=max(1, max_active_runs), thread_name_prefix="jarvis-workflow-run")

    def close(self) -> None:
        for active in list(self._active.values()):
            active.cancel.set()
        self._run_pool.shutdown(wait=False, cancel_futures=True)
        self._step_pool.shutdown(wait=False, cancel_futures=True)

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
        self, owner_id: UUID, workflow_id: UUID, inputs: dict[str, Any] | None = None,
        approval_id: str | None = None, wait: bool = True,
    ) -> WorkflowRunDetail:
        definition = self.get_definition(owner_id, workflow_id)
        if not definition:
            raise WorkflowExecutionError(f"Workflow definition {workflow_id} not found.")
        initial_inputs = dict(inputs or {})
        writes = [step for step in definition.dag_spec.steps if not is_read_only_step(step)]
        if not writes:
            return self._start_run(owner_id, definition, initial_inputs, wait=wait)
        material = json.dumps({"workflow_id": str(definition.id), "dag": definition.dag_spec.model_dump(mode="json"),
                               "inputs": initial_inputs}, sort_keys=True, default=str)
        names = ", ".join(f"{step.step_id}:{step.action or step.step_type.value}" for step in writes)
        proposal = ApprovalProposal(
            category="workflow", action="workflow.run", target=f"{definition.name}: {names}"[:300],
            reason=f"Run workflow '{definition.name}'"[:500], risk="medium",
            expected_side_effects=f"Runs {len(definition.dag_spec.steps)} step(s), {len(writes)} of which write data: "
                                  f"{names}."[:300],
            scope_material=hashlib.sha256(material.encode("utf-8")).hexdigest(),
        )
        return run_approved(
            self.approval_service, owner_id, proposal, approval_id,
            lambda: self._start_run(owner_id, definition, initial_inputs, wait=wait),
            # A background run is audited as started; its own finish event records the outcome.
            succeeded=lambda detail: detail.run.status in (WorkflowStatus.COMPLETED, WorkflowStatus.RUNNING),
        )

    def _start_run(self, owner_id: UUID, definition: WorkflowDefinition, initial_inputs: dict[str, Any], *,
                   wait: bool) -> WorkflowRunDetail:
        with self._active_lock:
            if len(self._active) >= self.max_active_runs:
                raise WorkflowStateError(f"{len(self._active)} workflow runs are already active; try again later.")
            run_id = uuid4()
            active = self._active[run_id] = _ActiveRun()
        try:
            now = datetime.now(timezone.utc)
            step_runs = [
                WorkflowStepRun(
                    id=uuid4(), run_id=run_id, step_id=step.step_id, step_name=step.name, status=StepStatus.PENDING,
                    inputs=dict(step.parameters), outputs={}, error_message=None, recovery_metadata={},
                    started_at=None, completed_at=None,
                )
                for step in definition.dag_spec.steps
            ]
            run = WorkflowRun(
                id=run_id, workflow_id=definition.id, owner_id=owner_id, status=WorkflowStatus.RUNNING,
                inputs=initial_inputs, outputs={}, error_message=None, created_at=now, started_at=now,
                completed_at=None,
            )
            self.store.create_run(run, step_runs)
        except Exception:
            self._finish_active(run_id)
            raise
        self._audit(
            owner_id,
            "workflow.run.start",
            f"workflow:run:{run.id}",
            {"workflow_id": str(definition.id), "steps": len(step_runs), "background": not wait},
        )
        if wait:
            self._run(owner_id, run_id, definition, initial_inputs, active)
        else:
            context = contextvars.copy_context()
            self._run_pool.submit(context.run, self._run, owner_id, run_id, definition, initial_inputs, active)
        return self.get_run(owner_id, run_id)

    def _finish_active(self, run_id: UUID) -> None:
        with self._active_lock:
            active = self._active.pop(run_id, None)
        if active is not None:
            active.done.set()

    def _run(self, owner_id: UUID, run_id: UUID, definition: WorkflowDefinition, inputs: dict[str, Any],
             active: _ActiveRun) -> None:
        try:
            try:
                run_outputs, run_status, error_msg = self._execute_dag(
                    owner_id=owner_id, run_id=run_id, dag_spec=definition.dag_spec, run_inputs=inputs,
                    cancel=active.cancel, deadline=monotonic() + self.run_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - a run must never stay "running" after an internal error
                logger.error("Workflow run %s failed internally: %s", run_id, type(exc).__name__)
                run_outputs, run_status = {}, WorkflowStatus.FAILED
                error_msg = f"Internal error ({type(exc).__name__}); completed steps were kept."
            self.store.update_run(owner_id, run_id, {
                "status": run_status, "outputs": run_outputs, "error_message": error_msg,
                "completed_at": datetime.now(timezone.utc),
            })
            self._audit(
                owner_id,
                "workflow.run.finish",
                f"workflow:run:{run_id}",
                {"status": run_status.value, "has_error": bool(error_msg)},
            )
        finally:
            self._finish_active(run_id)

    def start_recovery(self, owner_id: UUID) -> threading.Thread:
        """Run ``recover_interrupted_runs`` in a background thread; ``recovery_done`` is set when it ends."""
        def work() -> None:
            try:
                self.recover_interrupted_runs(owner_id)
            except Exception as exc:  # noqa: BLE001 - the runs stay visible as they are
                logger.warning("Interrupted workflow runs could not be recovered: %s", type(exc).__name__)
            finally:
                self.recovery_done.set()

        thread = threading.Thread(target=work, name="jarvis-workflow-recovery", daemon=True)
        thread.start()
        return thread

    def recover_interrupted_runs(self, owner_id: UUID) -> int:
        """Mark runs left running or pending by a previous process as failed (v4.4)."""
        recovered = 0
        now = datetime.now(timezone.utc)
        for state in (WorkflowStatus.RUNNING, WorkflowStatus.PENDING):
            items, _ = self.store.list_runs(owner_id, workflow_id=None, status=state, limit=200, offset=0)
            for run in items:
                if run.id in self._active:
                    continue
                self.store.update_run(owner_id, run.id, {
                    "status": WorkflowStatus.FAILED, "completed_at": now,
                    "error_message": "Interrupted: the server stopped while this run was in progress. Completed "
                                     "steps were kept; use compensate to undo them.",
                })
                for step in self.store.get_run_steps(run.id):
                    if step.status in (StepStatus.PENDING, StepStatus.RUNNING):
                        self.store.update_step_run(run.id, step.step_id, {
                            "status": StepStatus.SKIPPED if step.status == StepStatus.PENDING else StepStatus.FAILED,
                            "error_message": "Interrupted by a server restart; the outcome is unknown."
                            if step.status == StepStatus.RUNNING else "Interrupted by a server restart.",
                            "completed_at": now,
                        })
                recovered += 1
        if recovered:
            self._audit(owner_id, "workflow.run.recover", "workflow:runs", {"interrupted_runs": recovered})
        return recovered

    def _execute_dag(
        self,
        owner_id: UUID,
        run_id: UUID,
        dag_spec: DAGSpec,
        run_inputs: dict[str, Any],
        cancel: threading.Event | None = None,
        deadline: float | None = None,
    ) -> tuple[dict[str, Any], WorkflowStatus, str | None]:
        cancel = cancel or threading.Event()
        deadline = deadline if deadline is not None else monotonic() + self.run_timeout_seconds
        cancelled = False
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

                if cancel.is_set() or cancelled:
                    cancelled = True
                    step_statuses[step_def.step_id] = StepStatus.SKIPPED
                    self.store.update_step_run(run_id, step_def.step_id, {
                        "status": StepStatus.SKIPPED, "error_message": "Run cancelled by the owner.",
                        "recovery_metadata": {"compensation_status": "none", "skipped_reason": "cancelled"},
                        "completed_at": datetime.now(timezone.utc),
                    })
                    continue

                if not has_failure and monotonic() >= deadline:
                    has_failure = True
                    first_error = first_error or f"The run exceeded its {self.run_timeout_seconds:g} s deadline."

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

                # Execute step with a timeout; only read-only steps are retried (v4.4)
                tier = classify_action_tier(step_def.step_type.value, step_def.action)
                read_only = is_read_only_step(step_def)
                max_attempts = MAX_STEP_RETRIES + 1 if read_only else 1
                attempt = 0
                attempt_timestamps: list[str] = []
                step_success = False
                outcome_unknown = False
                outputs: dict[str, Any] = {}
                err_text: str | None = None

                while attempt < max_attempts:
                    attempt += 1
                    ts = datetime.now(timezone.utc)
                    attempt_timestamps.append(ts.isoformat())

                    self.store.update_step_run(
                        run_id,
                        step_def.step_id,
                        {"status": StepStatus.RUNNING, "started_at": ts},
                    )

                    remaining = deadline - monotonic()
                    limited_by_deadline = remaining < self.step_timeout_seconds
                    timeout = max(0.01, min(self.step_timeout_seconds, remaining))
                    future = self._step_pool.submit(
                        contextvars.copy_context().run, self._execute_step,
                        owner_id=owner_id, step=step_def, run_inputs=run_inputs, prior_outputs=step_outputs,
                    )
                    try:
                        outputs = future.result(timeout=timeout)
                        step_success = True
                        break
                    except FutureTimeout:
                        future.cancel()
                        err_text = (f"The run exceeded its {self.run_timeout_seconds:g} s deadline during this step."
                                    if limited_by_deadline else f"The step timed out after {timeout:.3g} s.")
                        if not read_only:
                            outcome_unknown = True
                            err_text += " It writes data and may still complete, so its outcome is unknown."
                            break
                        if limited_by_deadline:
                            break  # no time is left for a retry
                    except StepUnavailableError as exc:
                        err_text = str(exc)
                        break  # permanent: retrying cannot make a missing capability appear
                    except Exception as exc:
                        err_text = str(exc)
                        logger.warning(
                            "Step '%s' attempt %d/%d failed: %s",
                            step_def.step_id,
                            attempt,
                            max_attempts,
                            err_text,
                        )
                    if attempt < max_attempts and self.backoff_enabled:
                        delay = self.backoff_schedule[min(attempt - 1, len(self.backoff_schedule) - 1)]
                        if cancel.wait(min(delay, max(0.0, deadline - monotonic()))):
                            err_text = "Run cancelled by the owner during a retry wait."
                            break

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
                                "compensation_status": "manual_required" if outcome_unknown else "none",
                                "attempt_count": attempt,
                                "attempt_timestamps": attempt_timestamps,
                                "rollback_tier": tier.value,
                                "failure_reason": err_text,
                                "outcome_unknown": outcome_unknown,
                                "compensation_log": [],
                            },
                            "completed_at": datetime.now(timezone.utc),
                        },
                    )

        if cancelled or cancel.is_set():
            kept = [step.step_id for step in completed_step_defs if not is_read_only_step(step)]
            outputs = {"steps": step_outputs,
                       "recovery": {"verdict": "cancelled_not_compensated", "completed_write_steps": kept}}
            message = "Run cancelled by the owner."
            if kept:
                message += f" Completed write steps were kept ({', '.join(kept)}); use compensate to undo them."
            return outputs, WorkflowStatus.CANCELLED, message

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

        # ------------------- TOOLS (v4.4: through the read-only ToolRuntime and its policy)
        if step.step_type == StepType.TOOL and self.tool_runtime is not None:
            return self._invoke_tool(owner_id, step, merged_params)
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

    def _invoke_tool(self, owner_id: UUID, step: WorkflowStepDefinition, params: dict[str, Any]) -> dict[str, Any]:
        from jarvis.tools.models import ToolStatus

        if self.tool_context_factory is None:
            raise StepUnavailableError("The tool context is not configured; nothing was executed.")
        result = self.tool_runtime.invoke(owner_id, step.action, params, self.tool_context_factory(owner_id))
        if result.status == ToolStatus.SUCCEEDED:
            output = result.output.model_dump(mode="json") if hasattr(result.output, "model_dump") else result.output
            return {"result": output, "tool_id": result.tool_id, "evidence_count": len(result.evidence),
                    "untrusted_content": result.untrusted_content}
        reason = result.error.message if result.error else result.status.value
        if result.retryable:
            raise WorkflowExecutionError(f"Tool '{step.action}' {result.status.value}: {reason}")
        raise StepUnavailableError(f"Tool '{step.action}' {result.status.value}: {reason}; nothing was executed.")

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

        # Tier 1: Idempotent - no compensation needed (v4.4: also every read-only step, such as read-only tools)
        if tier == RollbackTier.TIER_1_IDEMPOTENT or is_read_only_step(step_def):
            log.append({
                "action": "skip",
                "timestamp": now.isoformat(),
                "status": "success",
                "details": {"reason": "Read-only or telemetry step; there is nothing to undo."},
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

            if act == "research.documents.parse" and "document_id" in step_output:
                if self.research_service:
                    removed = self.research_service.delete_document(owner_id, UUID(step_output["document_id"]))
                    log.append({
                        "action": "delete_document",
                        "timestamp": now.isoformat(),
                        "status": "success",
                        "details": {"deleted_document_id": step_output["document_id"], "already_removed": not removed},
                    })
                    return "compensated", log

            # v4.4: a write with no automatic undo is reported honestly instead of as "compensated".
            log.append({
                "action": "manual_undo_required",
                "timestamp": now.isoformat(),
                "status": "manual_required",
                "details": {"reason": f"No automatic undo exists for '{step_def.action}'; check and undo it manually."},
            })
            return "manual_required", log

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
        """Undo the completed steps of a failed or cancelled run; a second call changes nothing (v4.4)."""
        detail = self.get_run(owner_id, run_id)
        if not detail:
            raise WorkflowExecutionError(f"Run {run_id} not found.")
        if detail.run.status not in (WorkflowStatus.FAILED, WorkflowStatus.CANCELLED):
            raise WorkflowStateError(f"Only failed or cancelled runs can be compensated; this run is "
                                     f"{detail.run.status.value}.")
        definition = self.get_definition(owner_id, detail.run.workflow_id)
        step_defs = {step.step_id: step for step in definition.dag_spec.steps} if definition else {}
        summary: list[dict[str, Any]] = []
        overall = "compensated"
        with self._compensation_lock:
            detail = self.get_run(owner_id, run_id)  # re-read under the lock
            completed_steps = [s for s in detail.steps if s.status == StepStatus.COMPLETED]
            for s in reversed(completed_steps):
                meta = dict(s.recovery_metadata or {})
                if meta.get("compensation_status") == "compensated":
                    summary.append({"step_id": s.step_id, "status": "compensated", "log": [],
                                    "note": "already compensated; nothing was done again"})
                    continue
                step_def = step_defs.get(s.step_id)
                if step_def is None:
                    c_status, c_log = "manual_required", [{
                        "action": "manual_undo_required", "timestamp": datetime.now(timezone.utc).isoformat(),
                        "status": "manual_required",
                        "details": {"reason": "The workflow definition no longer exists, so the step's action is unknown."},
                    }]
                else:
                    c_status, c_log = self._compensate_step(owner_id, step_def, s.outputs)
                if c_status != "compensated" and overall != "manual_required":
                    overall = c_status
                meta["compensation_status"] = c_status
                meta["compensation_log"] = c_log
                self.store.update_step_run(run_id, s.step_id, {"recovery_metadata": meta})
                summary.append({"step_id": s.step_id, "status": c_status, "log": c_log})
        self._audit(owner_id, "workflow.run.compensate", f"workflow:run:{run_id}", {"status": overall})
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

        active = self._active.get(run_id)
        if active is not None:
            # v4.4: a live run stops itself before its next step (or during a retry wait); the runner records it.
            active.cancel.set()
            self._audit(owner_id, "workflow.run.cancel", f"workflow:run:{run_id}", {"cancelled": True, "live": True})
            active.done.wait(timeout=2.0)
            return self.get_run(owner_id, run_id)

        # No runner owns this run (for example after a restart): record the cancellation directly.
        now = datetime.now(timezone.utc)
        updated_run = self.store.update_run(
            owner_id,
            run_id,
            {"status": WorkflowStatus.CANCELLED, "completed_at": now, "error_message": "Run cancelled by the owner."},
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
                        "error_message": "Run cancelled by the owner.",
                        "completed_at": now,
                    },
                )

        self._audit(
            owner_id,
            "workflow.run.cancel",
            f"workflow:run:{run_id}",
            {"cancelled": True, "live": False},
        )
        final_steps = self.store.get_run_steps(run_id)
        return WorkflowRunDetail(run=updated_run, steps=final_steps)
