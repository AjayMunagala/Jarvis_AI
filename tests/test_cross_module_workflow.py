"""Unit and integration tests for v3.8 Cross-Module Workflows, Bounded Retries, and Self-Healing."""

from uuid import UUID, uuid4
import pytest

from approval_flow import approve_if_required, memory_approvals
from jarvis.assistant.models import TaskCreateRequest
from jarvis.assistant.service import AssistantService
from jarvis.assistant.stores import MemoryAssistantStore
from jarvis.finance.service import FinanceService
from jarvis.finance.stores import MemoryFinanceStore
from jarvis.scheduler.service import SchedulerService
from jarvis.scheduler.stores import MemorySchedulerStore
from jarvis.workflow.models import (
    DAGSpec,
    StepStatus,
    StepType,
    WorkflowDefinitionCreateRequest,
    WorkflowStatus,
    WorkflowStepDefinition,
)
from jarvis.workflow.recovery import (
    RollbackTier,
    classify_action_tier,
)
from jarvis.workflow.service import WorkflowService
from jarvis.workflow.stores import MemoryWorkflowStore

TEST_OWNER = uuid4()


@pytest.fixture
def workflow_env():
    wf_store = MemoryWorkflowStore()
    ast_store = MemoryAssistantStore()
    fin_store = MemoryFinanceStore()
    sch_store = MemorySchedulerStore()

    assistant = AssistantService(ast_store)
    finance = FinanceService(fin_store)
    scheduler = SchedulerService(sch_store)

    flaky_counts = {"attempts": 0}

    def flaky_tool(params):
        flaky_counts["attempts"] += 1
        if flaky_counts["attempts"] < 2:
            raise RuntimeError("Transient network timeout")
        return {"flaky": "recovered"}

    def failing_tool(params):
        raise RuntimeError("Permanent downstream crash")

    handlers = {
        "tool.flaky": flaky_tool,
        "tool.failing": failing_tool,
    }

    service = WorkflowService(
        store=wf_store,
        assistant_service=assistant,
        finance_service=finance,
        scheduler_service=scheduler,
        tool_handlers=handlers,
        read_only_tool_handlers=frozenset(handlers),  # v4.4: in-process handlers must be declared read-only
        backoff_enabled=False,  # Fast tests
    )
    service.approval_service = approvals = memory_approvals()  # v4.4: write steps need an approved request

    return {
        "service": service,
        "assistant": assistant,
        "finance": finance,
        "scheduler": scheduler,
        "flaky_counts": flaky_counts,
        "approvals": approvals,
    }


def test_action_tier_classification():
    assert classify_action_tier("tool", "tasks.list") == RollbackTier.TIER_1_IDEMPOTENT
    assert classify_action_tier("tool", "finance.snapshot") == RollbackTier.TIER_1_IDEMPOTENT
    assert classify_action_tier("tool", "tasks.create") == RollbackTier.TIER_2_COMPENSATING
    assert classify_action_tier("tool", "finance.accounts.create") == RollbackTier.TIER_2_COMPENSATING
    assert classify_action_tier("tool", "quarantine.file") == RollbackTier.TIER_3_REVERSIBLE
    assert classify_action_tier("tool", "scheduler.notifications.send") == RollbackTier.TIER_4_MANUAL


def test_cross_module_workflow_success(workflow_env):
    service = workflow_env["service"]

    dag = DAGSpec(
        steps=[
            WorkflowStepDefinition(
                step_id="step_acc",
                name="Create Treasury Account",
                step_type=StepType.TOOL,
                action="finance.accounts.create",
                parameters={"name": "Treasury", "balance": 15000.0, "account_type": "checking"},
            ),
            WorkflowStepDefinition(
                step_id="step_task",
                name="Create Task for Owner",
                step_type=StepType.ASSISTANT,
                action="tasks.create",
                parameters={"title": "Review Treasury Allocation"},
                depends_on=["step_acc"],
            ),
            WorkflowStepDefinition(
                step_id="step_transform",
                name="Transform Results",
                step_type=StepType.TRANSFORM,
                depends_on=["step_task"],
            ),
        ]
    )

    definition = service.create_definition(
        TEST_OWNER,
        WorkflowDefinitionCreateRequest(name="Cross-Module Allocation", dag_spec=dag),
    )

    run_detail = approve_if_required(workflow_env["approvals"], TEST_OWNER,
                                     lambda approval_id: service.trigger_run(TEST_OWNER, definition.id, None, approval_id))
    assert run_detail.run.status == WorkflowStatus.COMPLETED
    assert len(run_detail.steps) == 3
    for s in run_detail.steps:
        assert s.status == StepStatus.COMPLETED
        assert s.recovery_metadata["attempt_count"] == 1


def test_bounded_retry_recovery(workflow_env):
    service = workflow_env["service"]

    dag = DAGSpec(
        steps=[
            WorkflowStepDefinition(
                step_id="step_flaky",
                name="Flaky Step",
                step_type=StepType.TOOL,
                action="tool.flaky",
            ),
        ]
    )
    definition = service.create_definition(
        TEST_OWNER,
        WorkflowDefinitionCreateRequest(name="Retry Workflow", dag_spec=dag),
    )

    run_detail = approve_if_required(workflow_env["approvals"], TEST_OWNER,
                                     lambda approval_id: service.trigger_run(TEST_OWNER, definition.id, None, approval_id))
    assert run_detail.run.status == WorkflowStatus.COMPLETED
    step_flaky = run_detail.steps[0]
    assert step_flaky.status == StepStatus.COMPLETED
    assert step_flaky.recovery_metadata["attempt_count"] == 2
    assert len(step_flaky.recovery_metadata["attempt_timestamps"]) == 2


def test_failure_exhaustion_triggers_backward_compensation(workflow_env):
    service = workflow_env["service"]
    assistant = workflow_env["assistant"]
    finance = workflow_env["finance"]

    dag = DAGSpec(
        steps=[
            WorkflowStepDefinition(
                step_id="step_acc",
                name="Create Account",
                step_type=StepType.TOOL,
                action="finance.accounts.create",
                parameters={"name": "Temporary Account", "balance": 100.0, "account_type": "checking"},
            ),
            WorkflowStepDefinition(
                step_id="step_task",
                name="Create Task",
                step_type=StepType.ASSISTANT,
                action="tasks.create",
                parameters={"title": "Temporary Task"},
                depends_on=["step_acc"],
            ),
            WorkflowStepDefinition(
                step_id="step_crash",
                name="Crashing Step",
                step_type=StepType.TOOL,
                action="tool.failing",
                depends_on=["step_task"],
            ),
        ]
    )

    definition = service.create_definition(
        TEST_OWNER,
        WorkflowDefinitionCreateRequest(name="Compensating Workflow", dag_spec=dag),
    )

    run_detail = approve_if_required(workflow_env["approvals"], TEST_OWNER,
                                     lambda approval_id: service.trigger_run(TEST_OWNER, definition.id, None, approval_id))
    assert run_detail.run.status == WorkflowStatus.FAILED
    assert "Recovery: failed_compensated" in run_detail.run.error_message
    assert run_detail.run.outputs["recovery"]["verdict"] == "failed_compensated"

    # Verify step_acc and step_task were compensated (deleted)
    accs_page = finance.list_accounts(TEST_OWNER)
    assert accs_page.total == 0

    tasks_page = assistant.list_tasks(TEST_OWNER)
    assert tasks_page.total == 0

    # Verify recovery metadata records compensation status
    steps_by_id = {s.step_id: s for s in run_detail.steps}
    assert steps_by_id["step_acc"].recovery_metadata["compensation_status"] == "compensated"
    assert steps_by_id["step_task"].recovery_metadata["compensation_status"] == "compensated"
    assert steps_by_id["step_crash"].recovery_metadata["attempt_count"] == 3


def test_tier_4_manual_intervention_dead_letter(workflow_env):
    service = workflow_env["service"]

    dag = DAGSpec(
        steps=[
            WorkflowStepDefinition(
                step_id="step_notif",
                name="Send Proactive Alert",
                step_type=StepType.TOOL,
                action="scheduler.notifications.send",
                parameters={"category": "workflow", "title": "Deployment Warning", "message": "Notice"},
            ),
            WorkflowStepDefinition(
                step_id="step_fail",
                name="Crash Step",
                step_type=StepType.TOOL,
                action="tool.failing",
                depends_on=["step_notif"],
            ),
        ]
    )

    definition = service.create_definition(
        TEST_OWNER,
        WorkflowDefinitionCreateRequest(name="Tier 4 Dead Letter WF", dag_spec=dag),
    )

    run_detail = approve_if_required(workflow_env["approvals"], TEST_OWNER,
                                     lambda approval_id: service.trigger_run(TEST_OWNER, definition.id, None, approval_id))
    assert run_detail.run.status == WorkflowStatus.FAILED
    assert run_detail.run.outputs["recovery"]["verdict"] == "failed_uncompensated_manual_required"

    # Dead letters endpoint lists the run step
    dead_letters = service.get_dead_letters(TEST_OWNER)
    assert len(dead_letters) >= 1
    assert any(dl["step_id"] == "step_notif" for dl in dead_letters)
