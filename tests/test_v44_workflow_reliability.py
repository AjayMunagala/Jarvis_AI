"""v4.4 reliable workflow execution: timeouts, cancel, safe retries, approval, compensation, atomic finance."""

import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from approval_flow import approve_if_required, approve_over_http, memory_approvals
from jarvis.assistant.service import AssistantService
from jarvis.audit.models import ApprovalProposal
from jarvis.assistant.stores import MemoryAssistantStore
from jarvis.core.config import Settings
from jarvis.finance.models import AccountCreateRequest, TransactionCreateRequest
from jarvis.finance.service import FinanceService
from jarvis.finance.stores import MemoryFinanceStore
from jarvis.main import create_app
from jarvis.workflow.models import (DAGSpec, StepStatus, WorkflowDefinitionCreateRequest, WorkflowRun,
                                    WorkflowStatus, WorkflowStepDefinition, WorkflowStepRun)
from jarvis.workflow.service import WorkflowService, WorkflowStateError, is_read_only_step
from jarvis.workflow.stores import MemoryWorkflowStore

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TOKEN = "v44-workflow-reliability-token-32-chars"


def step(step_id, step_type="noop", action="", depends_on=()):
    return {"step_id": step_id, "name": step_id, "step_type": step_type, "action": action,
            "depends_on": list(depends_on)}


def define(service, *steps):
    return service.create_definition(OWNER, WorkflowDefinitionCreateRequest(name="v44", dag_spec=DAGSpec(steps=list(steps))))


def run(service, definition, wait=True):
    return approve_if_required(service.approval_service, OWNER,
                               lambda approval_id: service.trigger_run(OWNER, definition.id, {}, approval_id, wait))


class SlowAssistant:
    """A write step (tasks.create) that blocks until released."""

    def __init__(self):
        self.release = threading.Event()
        self.calls = 0

    def create_task(self, owner_id, request):
        self.calls += 1
        self.release.wait(5)
        raise RuntimeError("released after the test")


def make_service(**overrides):
    values = dict(backoff_enabled=False, step_timeout_seconds=5.0, run_timeout_seconds=30.0)
    values.update(overrides)
    # Test tool handlers are declared read-only, as the ToolRuntime path is (v4.4 finding 6).
    values.setdefault("read_only_tool_handlers", frozenset(values.get("tool_handlers", {})))
    service = WorkflowService(MemoryWorkflowStore(), **values)
    service.approval_service = memory_approvals()
    return service


def test_read_only_classification_follows_the_action_not_the_step_type():
    def s(step_type, action):
        return WorkflowStepDefinition(step_id="s", name="s", step_type=step_type, action=action)

    assert is_read_only_step(s("noop", "")) and is_read_only_step(s("transform", ""))
    assert is_read_only_step(s("tool", "knowledge.search")) and is_read_only_step(s("assistant", "tasks.list"))
    assert is_read_only_step(s("tool", "repo.read_file"))  # runs through the read-only tool runtime
    assert not is_read_only_step(s("tool", "finance.accounts.create"))  # a service write, whatever the type
    assert not is_read_only_step(s("assistant", "anything.else"))
    assert not is_read_only_step(s("tool", "health.diagnostic.run"))


def test_write_step_that_times_out_is_not_retried_and_is_marked_unknown():
    slow = SlowAssistant()
    service = make_service(assistant_service=slow, step_timeout_seconds=0.2, backoff_enabled=True)
    try:
        detail = run(service, define(service, step("w", "assistant", "tasks.create")))
    finally:
        slow.release.set()
    failed = detail.steps[0]
    assert detail.run.status == WorkflowStatus.FAILED and slow.calls == 1
    assert failed.recovery_metadata["attempt_count"] == 1 and failed.recovery_metadata["outcome_unknown"] is True
    assert failed.recovery_metadata["compensation_status"] == "manual_required"
    assert "outcome is unknown" in failed.error_message


def test_only_read_only_steps_are_retried():
    attempts = {"read": 0}

    def flaky_read(params):
        attempts["read"] += 1
        if attempts["read"] == 1:
            raise RuntimeError("transient")
        return {"ok": True}

    class FailingAssistant:
        calls = 0

        def create_task(self, owner_id, request):
            FailingAssistant.calls += 1
            raise RuntimeError("write failed after a partial effect")

    service = make_service(tool_handlers={"tool.read": flaky_read}, assistant_service=FailingAssistant())
    read = run(service, define(service, step("r", "tool", "tool.read")))
    write = run(service, define(service, step("w", "assistant", "tasks.create")))
    assert read.run.status == WorkflowStatus.COMPLETED and read.steps[0].recovery_metadata["attempt_count"] == 2
    assert write.run.status == WorkflowStatus.FAILED and FailingAssistant.calls == 1


def test_cancel_stops_a_live_background_run_before_its_next_step():
    started, release = threading.Event(), threading.Event()

    def blocking_read(params):
        started.set()
        release.wait(5)
        return {"ok": True}

    service = make_service(tool_handlers={"tool.block": blocking_read, "tool.after": lambda params: {"ran": True}})
    definition = define(service, step("first", "tool", "tool.block"), step("second", "tool", "tool.after", ["first"]))
    detail = run(service, definition, wait=False)
    assert detail.run.status == WorkflowStatus.RUNNING
    assert started.wait(5)
    threading.Timer(0.3, release.set).start()
    cancelled = service.cancel_run(OWNER, detail.run.id)
    assert cancelled.run.status == WorkflowStatus.CANCELLED
    steps = {item.step_id: item for item in cancelled.steps}
    assert steps["first"].status == StepStatus.COMPLETED and steps["second"].status == StepStatus.SKIPPED
    assert steps["second"].error_message == "Run cancelled by the owner."


def test_cancel_interrupts_a_retry_wait_instead_of_sleeping_it_out():
    def always_fails(params):
        raise RuntimeError("transient")

    service = make_service(tool_handlers={"tool.fail": always_fails}, backoff_enabled=True, backoff_schedule=[30.0])
    detail = run(service, define(service, step("r", "tool", "tool.fail")), wait=False)
    time.sleep(0.2)
    began = time.monotonic()
    cancelled = service.cancel_run(OWNER, detail.run.id)
    assert cancelled.run.status == WorkflowStatus.CANCELLED and time.monotonic() - began < 2.5


def test_run_deadline_stops_remaining_steps():
    def slow_read(params):
        time.sleep(0.3)
        return {"ok": True}

    service = make_service(tool_handlers={"tool.slow": slow_read}, run_timeout_seconds=0.4)
    definition = define(service, step("a", "tool", "tool.slow"), step("b", "tool", "tool.slow", ["a"]),
                        step("c", "tool", "tool.slow", ["b"]))
    detail = run(service, definition)
    assert detail.run.status == WorkflowStatus.FAILED and "deadline" in detail.run.error_message
    assert detail.steps[-1].status == StepStatus.SKIPPED


def test_runs_interrupted_by_a_restart_are_marked_failed():
    store = MemoryWorkflowStore()
    service = WorkflowService(store)
    definition = define(service, step("a"), step("b", depends_on=["a"]))
    now = datetime.now(timezone.utc)
    run_id = uuid4()
    store.create_run(
        WorkflowRun(id=run_id, workflow_id=definition.id, owner_id=OWNER, status=WorkflowStatus.RUNNING, inputs={},
                    outputs={}, error_message=None, created_at=now, started_at=now, completed_at=None),
        [WorkflowStepRun(id=uuid4(), run_id=run_id, step_id=sid, step_name=sid, status=status, inputs={}, outputs={},
                         error_message=None, recovery_metadata={}, started_at=None, completed_at=None)
         for sid, status in (("a", StepStatus.RUNNING), ("b", StepStatus.PENDING))])
    assert WorkflowService(store).recover_interrupted_runs(OWNER) == 1
    detail = service.get_run(OWNER, run_id)
    steps = {item.step_id: item for item in detail.steps}
    assert detail.run.status == WorkflowStatus.FAILED and "Interrupted" in detail.run.error_message
    assert steps["a"].status == StepStatus.FAILED and "outcome is unknown" in steps["a"].error_message
    assert steps["b"].status == StepStatus.SKIPPED


def test_compensation_is_honest_and_happens_once():
    assistant = AssistantService(MemoryAssistantStore())

    def fail(params):
        raise RuntimeError("downstream failure")

    service = make_service(assistant_service=assistant, tool_handlers={"tool.fail": fail})
    definition = define(service, step("task", "assistant", "tasks.create"),
                        step("boom", "tool", "tool.fail", ["task"]))
    detail = run(service, definition)
    assert detail.run.status == WorkflowStatus.FAILED
    assert assistant.list_tasks(OWNER).total == 0  # automatic compensation deleted the task
    first = service.compensate_run(OWNER, detail.run.id)
    assert first["summary"][0]["note"].startswith("already compensated")
    with pytest.raises(WorkflowStateError):
        service.compensate_run(OWNER, run(service, define(service, step("n"))).run.id)  # completed runs: refused


def test_a_write_without_an_undo_is_reported_manual_required():
    class Health:
        def run_diagnostics(self, owner_id):
            class Report:
                telemetry = type("T", (), {"id": uuid4()})()
                recommendations = ["ok"]
            return Report()

    def fail(params):
        raise RuntimeError("downstream failure")

    service = make_service(health_service=Health(), tool_handlers={"tool.fail": fail})
    detail = run(service, define(service, step("diag", "tool", "health.diagnostic.run"),
                                 step("boom", "tool", "tool.fail", ["diag"])))
    diag = next(item for item in detail.steps if item.step_id == "diag")
    assert diag.recovery_metadata["compensation_status"] == "manual_required"
    assert "manual_required" in detail.run.error_message


def test_finance_transaction_and_balance_change_together():
    finance = FinanceService(MemoryFinanceStore())
    account = finance.create_account(OWNER, AccountCreateRequest(name="Cash", account_type="checking", institution="Bank", balance=100.0))
    with pytest.raises(ValueError):
        finance.record_transaction(OWNER, TransactionCreateRequest(account_id=uuid4(), amount=5.0, description="x"))
    assert finance.list_transactions(OWNER).total == 0
    tx = finance.record_transaction(OWNER, TransactionCreateRequest(account_id=account.id, amount=-40.0,
                                                                    description="groceries"))
    assert finance.get_account(OWNER, account.id).balance == 60.0
    assert finance.delete_transaction(OWNER, tx.id) is True and finance.delete_transaction(OWNER, tx.id) is False
    assert finance.get_account(OWNER, account.id).balance == 100.0  # reversed exactly once


def test_active_run_limit_is_enforced():
    release = threading.Event()
    service = make_service(tool_handlers={"tool.block": lambda params: release.wait(5) or {"ok": True}},
                           max_active_runs=1)
    definition = define(service, step("a", "tool", "tool.block"))
    try:
        run(service, definition, wait=False)
        with pytest.raises(WorkflowStateError):
            run(service, definition, wait=False)
    finally:
        release.set()


def test_workflow_service_never_sleeps_blindly():
    source = (Path(__file__).resolve().parents[1] / "jarvis" / "workflow" / "service.py").read_text(encoding="utf-8")
    assert "time.sleep" not in source


# ------------------------------------------------------------- API

def make_client():
    app = create_app(settings=Settings(_env_file=None, environment="test", log_level="ERROR",
                                       local_auth_token=SecretStr(TOKEN), owner_id=OWNER))
    return app, TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})


def test_api_write_workflow_waits_for_approval_and_read_only_runs_directly():
    app, client = make_client()
    with client:
        writes = client.post("/workflows/definitions", json={"name": "w", "dag_spec": {"steps": [
            step("t", "assistant", "tasks.create")]}}).json()
        reads = client.post("/workflows/definitions", json={"name": "r", "dag_spec": {"steps": [
            step("l", "assistant", "tasks.list")]}}).json()
        url = f"/workflows/definitions/{writes['id']}/run"
        waiting = client.post(url, json={})
        assert waiting.status_code == 202 and waiting.json()["approval"]["category"] == "workflow"
        assert client.get("/assistant/tasks").json()["total"] == 0
        ran = approve_over_http(client, lambda approval_id: client.post(
            url, json={"approval_id": approval_id} if approval_id else {}))
        assert ran.status_code == 201 and ran.json()["run"]["status"] == "completed"
        assert client.get("/assistant/tasks").json()["total"] == 1
        direct = client.post(f"/workflows/definitions/{reads['id']}/run", json={})
        assert direct.status_code == 201 and direct.json()["run"]["status"] == "completed"
        completed = client.post(f"/workflows/runs/{direct.json()['run']['id']}/compensate")
        assert completed.status_code == 409


def test_api_tool_steps_go_through_the_tool_runtime():
    app, client = make_client()
    with client:
        definition = client.post("/workflows/definitions", json={"name": "t", "dag_spec": {"steps": [
            step("t", "tool", "repo.status")]}}).json()
        result = client.post(f"/workflows/definitions/{definition['id']}/run", json={}).json()
    failed = result["steps"][0]
    assert result["run"]["status"] == "failed" and "disabled" in failed["error_message"]
    assert failed["recovery_metadata"]["attempt_count"] == 1


# ------------------------------------------------------------- review fixes

def test_a_write_step_that_never_started_is_not_reported_as_unknown():
    from concurrent.futures import ThreadPoolExecutor

    class CountingAssistant:
        calls = 0

        def create_task(self, owner_id, request):
            CountingAssistant.calls += 1
            return type("T", (), {"id": uuid4(), "title": "t"})()

    service = make_service(assistant_service=CountingAssistant(), step_timeout_seconds=0.2)
    release = threading.Event()
    service._step_pool = ThreadPoolExecutor(max_workers=1)
    service._step_pool.submit(release.wait, 5)  # the only worker is busy with a hung step
    try:
        detail = run(service, define(service, step("w", "assistant", "tasks.create")))
    finally:
        release.set()
    failed = detail.steps[0]
    assert CountingAssistant.calls == 0 and failed.status == StepStatus.FAILED
    assert failed.recovery_metadata["outcome_unknown"] is False and "did not start" in failed.error_message
    assert failed.recovery_metadata["compensation_status"] == "none"


def test_cancel_never_overwrites_a_run_that_finished_concurrently():
    service = make_service()
    detail = run(service, define(service, step("a")))
    assert detail.run.status == WorkflowStatus.COMPLETED
    real_get_run = service.store.get_run
    stale = detail.run.model_copy(update={"status": WorkflowStatus.RUNNING})
    service.store.get_run = lambda owner_id, run_id: stale  # the status was read just before the run finished
    try:
        service.cancel_run(OWNER, detail.run.id)
    finally:
        service.store.get_run = real_get_run
    assert service.get_run(OWNER, detail.run.id).run.status == WorkflowStatus.COMPLETED


def test_recovery_ignores_runs_started_by_this_process():
    store = MemoryWorkflowStore()
    service = WorkflowService(store)
    definition = define(service, step("a"))
    now = datetime.now(timezone.utc)
    run_id = uuid4()
    time.sleep(0.02)
    store.create_run(WorkflowRun(id=run_id, workflow_id=definition.id, owner_id=OWNER, status=WorkflowStatus.RUNNING,
                                 inputs={}, outputs={}, error_message=None, created_at=datetime.now(timezone.utc),
                                 started_at=now, completed_at=None), [])
    assert service.recover_interrupted_runs(OWNER) == 0
    assert service.get_run(OWNER, run_id).run.status == WorkflowStatus.RUNNING


def test_compensation_reports_unknown_outcome_writes():
    slow = SlowAssistant()
    service = make_service(assistant_service=slow, step_timeout_seconds=0.2)
    try:
        detail = run(service, define(service, step("w", "assistant", "tasks.create")))
    finally:
        slow.release.set()
    result = service.compensate_run(OWNER, detail.run.id)
    assert result["status"] == "manual_required" and result["summary"][0]["step_id"] == "w"


def test_a_full_engine_refuses_before_the_approval_is_used():
    release = threading.Event()
    service = make_service(tool_handlers={"tool.block": lambda params: release.wait(5) or {"ok": True}},
                           assistant_service=AssistantService(MemoryAssistantStore()), max_active_runs=1)
    busy = define(service, step("b", "tool", "tool.block"))
    writes = define(service, step("w", "assistant", "tasks.create"))
    try:
        run(service, busy, wait=False)
        approvals = service.approval_service
        request = approvals.request(OWNER, ApprovalProposal(
            category="workflow", action="workflow.run", target="x", reason="r", risk="medium",
            expected_side_effects="e", scope_material="unused"))
        approvals.decide(OWNER, request.approval_id, approve=True, note=None)
        with pytest.raises(WorkflowStateError):
            service.trigger_run(OWNER, writes.id, {}, request.approval_id)
        assert approvals.get(OWNER, request.approval_id).consumed_at is None
    finally:
        release.set()


def test_a_cancel_after_the_last_step_keeps_the_real_status():
    service = make_service()
    holder = {}

    def last_step(params):
        holder["cancel"].set()  # the cancel arrives while the final step is running
        return {"ok": True}

    service.tool_handlers = {"tool.last": last_step}
    service.read_only_tool_handlers = frozenset({"tool.last"})
    holder["cancel"] = threading.Event()
    dag = DAGSpec(steps=[step("only", "tool", "tool.last")])
    outputs, status, error = service._execute_dag(OWNER, uuid4(), dag, {}, cancel=holder["cancel"])
    assert status == WorkflowStatus.COMPLETED and error is None
