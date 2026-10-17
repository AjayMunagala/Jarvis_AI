"""Unit tests for the v3.2 Workflow Orchestration subsystem.

Covers:
- DAG validation and cycle detection (Kahn's topological sort)
- Topological level ordering for parallel execution
- MemoryWorkflowStore lifecycle and step runs
- WorkflowService execution, step dispatch, dependency skipping, failure handling, cancellation, and audit logging
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from jarvis.assistant.models import TaskCreateRequest
from jarvis.assistant.service import AssistantService
from jarvis.assistant.stores import MemoryAssistantStore
from jarvis.audit.service import AuditService
from jarvis.audit.stores import MemoryAuditStore
from jarvis.workflow.models import (
    DAGSpec,
    StepStatus,
    StepType,
    WorkflowDefinition,
    WorkflowDefinitionCreateRequest,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStepDefinition,
    WorkflowStepRun,
)
from jarvis.workflow.service import WorkflowExecutionError, WorkflowService
from jarvis.workflow.stores import MemoryWorkflowStore

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


# ===================================================================
# 1. DAG Validation & Cycle Detection
# ===================================================================

def test_valid_linear_dag():
    steps = [
        WorkflowStepDefinition(step_id="step_a", name="Step A", step_type=StepType.NOOP),
        WorkflowStepDefinition(step_id="step_b", name="Step B", step_type=StepType.NOOP, depends_on=["step_a"]),
        WorkflowStepDefinition(step_id="step_c", name="Step C", step_type=StepType.NOOP, depends_on=["step_b"]),
    ]
    dag = DAGSpec(steps=steps)
    levels = dag.topological_levels()
    assert len(levels) == 3
    assert [s.step_id for s in levels[0]] == ["step_a"]
    assert [s.step_id for s in levels[1]] == ["step_b"]
    assert [s.step_id for s in levels[2]] == ["step_c"]


def test_valid_branching_dag():
    # A -> B, A -> C, (B, C) -> D
    steps = [
        WorkflowStepDefinition(step_id="step_a", name="Step A", step_type=StepType.NOOP),
        WorkflowStepDefinition(step_id="step_b", name="Step B", step_type=StepType.NOOP, depends_on=["step_a"]),
        WorkflowStepDefinition(step_id="step_c", name="Step C", step_type=StepType.NOOP, depends_on=["step_a"]),
        WorkflowStepDefinition(step_id="step_d", name="Step D", step_type=StepType.NOOP, depends_on=["step_b", "step_c"]),
    ]
    dag = DAGSpec(steps=steps)
    levels = dag.topological_levels()
    assert len(levels) == 3
    assert [s.step_id for s in levels[0]] == ["step_a"]
    assert [s.step_id for s in levels[1]] == ["step_b", "step_c"]
    assert [s.step_id for s in levels[2]] == ["step_d"]


def test_dag_rejects_self_dependency():
    steps = [
        WorkflowStepDefinition(step_id="step_a", name="Step A", step_type=StepType.NOOP, depends_on=["step_a"]),
    ]
    with pytest.raises(ValidationError) as exc:
        DAGSpec(steps=steps)
    assert "cannot depend on itself" in str(exc.value)


def test_dag_rejects_missing_dependency():
    steps = [
        WorkflowStepDefinition(step_id="step_a", name="Step A", step_type=StepType.NOOP, depends_on=["nonexistent"]),
    ]
    with pytest.raises(ValidationError) as exc:
        DAGSpec(steps=steps)
    assert "depends on unknown step" in str(exc.value)


def test_dag_rejects_direct_cycle():
    # A -> B and B -> A
    steps = [
        WorkflowStepDefinition(step_id="step_a", name="Step A", step_type=StepType.NOOP, depends_on=["step_b"]),
        WorkflowStepDefinition(step_id="step_b", name="Step B", step_type=StepType.NOOP, depends_on=["step_a"]),
    ]
    with pytest.raises(ValidationError) as exc:
        DAGSpec(steps=steps)
    assert "Cycle detected" in str(exc.value)


def test_dag_rejects_indirect_cycle():
    # A -> B -> C -> A
    steps = [
        WorkflowStepDefinition(step_id="step_a", name="Step A", step_type=StepType.NOOP, depends_on=["step_c"]),
        WorkflowStepDefinition(step_id="step_b", name="Step B", step_type=StepType.NOOP, depends_on=["step_a"]),
        WorkflowStepDefinition(step_id="step_c", name="Step C", step_type=StepType.NOOP, depends_on=["step_b"]),
    ]
    with pytest.raises(ValidationError) as exc:
        DAGSpec(steps=steps)
    assert "Cycle detected" in str(exc.value)


# ===================================================================
# 2. MemoryWorkflowStore Tests
# ===================================================================

def test_memory_workflow_store_crud():
    store = MemoryWorkflowStore()
    now = datetime.now(timezone.utc)
    dag = DAGSpec(steps=[WorkflowStepDefinition(step_id="init", name="Init")])

    definition = WorkflowDefinition(
        id=uuid4(),
        owner_id=OWNER,
        name="Daily Briefing",
        description="Generates daily summary",
        dag_spec=dag,
        created_at=now,
        updated_at=now,
    )
    store.create_definition(definition)

    # Fetch
    fetched = store.get_definition(OWNER, definition.id)
    assert fetched is not None
    assert fetched.name == "Daily Briefing"

    # Isolation
    assert store.get_definition(OTHER, definition.id) is None

    # List
    items, total = store.list_definitions(OWNER)
    assert total == 1
    assert items[0].id == definition.id

    # Create run
    run_id = uuid4()
    run = WorkflowRun(
        id=run_id,
        workflow_id=definition.id,
        owner_id=OWNER,
        status=WorkflowStatus.PENDING,
        created_at=now,
    )
    step_run = WorkflowStepRun(
        id=uuid4(),
        run_id=run_id,
        step_id="init",
        step_name="Init",
        status=StepStatus.PENDING,
    )
    store.create_run(run, [step_run])

    # Fetch run
    fetched_run = store.get_run(OWNER, run_id)
    assert fetched_run is not None
    assert fetched_run.status == WorkflowStatus.PENDING
    steps = store.get_run_steps(run_id)
    assert len(steps) == 1
    assert steps[0].step_id == "init"

    # Update run and step
    store.update_run(OWNER, run_id, {"status": WorkflowStatus.COMPLETED})
    assert store.get_run(OWNER, run_id).status == WorkflowStatus.COMPLETED

    store.update_step_run(run_id, "init", {"status": StepStatus.COMPLETED})
    assert store.get_run_steps(run_id)[0].status == StepStatus.COMPLETED

    # Delete definition
    assert store.delete_definition(OWNER, definition.id) is True
    assert store.get_definition(OWNER, definition.id) is None


# ===================================================================
# 3. WorkflowService Execution Engine
# ===================================================================

def test_workflow_service_successful_execution():
    store = MemoryWorkflowStore()
    audit_store = MemoryAuditStore()
    audit_service = AuditService(audit_store)
    assistant_store = MemoryAssistantStore()
    assistant_service = AssistantService(assistant_store)

    # Pre-populate a task
    assistant_service.create_task(OWNER, TaskCreateRequest(title="Workflow Task"))

    service = WorkflowService(
        store,
        audit_service=audit_service,
        assistant_service=assistant_service,
    )

    dag = DAGSpec(
        steps=[
            WorkflowStepDefinition(step_id="step_1", name="Fetch Tasks", step_type=StepType.ASSISTANT, action="tasks.list"),
            WorkflowStepDefinition(step_id="step_2", name="Transform Data", step_type=StepType.TRANSFORM, depends_on=["step_1"]),
        ]
    )

    req = WorkflowDefinitionCreateRequest(name="Task Processor", dag_spec=dag)
    defn = service.create_definition(OWNER, req)
    assert defn.name == "Task Processor"

    # Trigger run
    detail = service.trigger_run(OWNER, defn.id, inputs={"mode": "test"})
    assert detail.run.status == WorkflowStatus.COMPLETED
    assert len(detail.steps) == 2
    assert detail.steps[0].status == StepStatus.COMPLETED
    assert detail.steps[0].outputs["total"] == 1
    assert detail.steps[1].status == StepStatus.COMPLETED
    assert detail.steps[1].outputs["transformed"] is True

    # Audit records emitted
    audit_events = audit_store.recent(OWNER, limit=10)
    assert any(e.event_type == "workflow.run.start" for e in audit_events)
    assert any(e.event_type == "workflow.run.finish" for e in audit_events)


def test_workflow_service_failure_and_skipping():
    store = MemoryWorkflowStore()

    def failing_tool(params):
        raise ValueError("Simulated tool crash")

    service = WorkflowService(store, tool_handlers={"crash_tool": failing_tool})

    dag = DAGSpec(
        steps=[
            WorkflowStepDefinition(step_id="bad_step", name="Crashing Step", step_type=StepType.TOOL, action="crash_tool"),
            WorkflowStepDefinition(step_id="downstream", name="Downstream Step", step_type=StepType.NOOP, depends_on=["bad_step"]),
        ]
    )

    defn = service.create_definition(OWNER, WorkflowDefinitionCreateRequest(name="Faulty DAG", dag_spec=dag))
    detail = service.trigger_run(OWNER, defn.id)

    assert detail.run.status == WorkflowStatus.FAILED
    assert "Simulated tool crash" in (detail.run.error_message or "")

    steps = {s.step_id: s for s in detail.steps}
    assert steps["bad_step"].status == StepStatus.FAILED
    assert "Simulated tool crash" in (steps["bad_step"].error_message or "")
    assert steps["downstream"].status == StepStatus.SKIPPED
    assert "did not complete successfully" in (steps["downstream"].error_message or "")


def test_workflow_service_cancellation():
    store = MemoryWorkflowStore()
    service = WorkflowService(store)

    dag = DAGSpec(
        steps=[
            WorkflowStepDefinition(step_id="step_a", name="Step A", step_type=StepType.NOOP),
        ]
    )

    defn = service.create_definition(OWNER, WorkflowDefinitionCreateRequest(name="Cancellable", dag_spec=dag))

    # Create a run in running state manually to test cancel
    run_id = uuid4()
    now = datetime.now(timezone.utc)
    run = WorkflowRun(id=run_id, workflow_id=defn.id, owner_id=OWNER, status=WorkflowStatus.RUNNING, created_at=now)
    step = WorkflowStepRun(id=uuid4(), run_id=run_id, step_id="step_a", step_name="Step A", status=StepStatus.RUNNING)
    store.create_run(run, [step])

    detail = service.cancel_run(OWNER, run_id)
    assert detail is not None
    assert detail.run.status == WorkflowStatus.CANCELLED
    assert detail.steps[0].status == StepStatus.SKIPPED
