"""v4.5 bounded proactive runtime: strict schedules, once-only due runs, reminders, quiet hours, throttle, runner."""

import threading
from datetime import datetime, time as clock_time, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from approval_flow import memory_approvals
from jarvis.assistant.models import ReminderCreateRequest, ReminderStatus
from jarvis.assistant.service import AssistantService
from jarvis.assistant.stores import MemoryAssistantStore
from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.scheduler.cron import CronExpression, ScheduleError, validate_schedule
from jarvis.scheduler.models import JobCreateRequest, NotificationCreateRequest
from jarvis.scheduler.runner import SchedulerRunner
from jarvis.scheduler.service import SchedulerService
from jarvis.scheduler.stores import MemorySchedulerStore
from jarvis.workflow.models import DAGSpec, WorkflowDefinitionCreateRequest
from jarvis.workflow.service import WorkflowService
from jarvis.workflow.stores import MemoryWorkflowStore

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TOKEN = "v45-scheduler-runtime-token-with-32-chars"
NOON = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)  # a Saturday


def make_service(**attributes):
    service = SchedulerService(MemorySchedulerStore())
    service.health_probe = lambda: {"database": "operational"}
    for key, value in attributes.items():
        setattr(service, key, value)
    return service


def make_due(service, job, when):
    service.store.save_job(job.model_copy(update={"next_run_at": when}))


def notifications(service, owner=OWNER):
    return service.store.list_notifications(owner, unread_only=False, limit=200, offset=0)[0]


# ------------------------------------------------------------- schedules

def test_schedules_are_validated_strictly():
    assert validate_schedule("interval", "60", now=NOON) == NOON + timedelta(seconds=60)
    assert CronExpression("0 9 * * 1-5").next_after(NOON) == datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
    for kind, expr in (("interval", "10"), ("interval", "soon"), ("cron", "0 9 * *"), ("cron", "0 25 * * *"),
                       ("once", "tomorrow"), ("once", "-5"), ("cron", "0 0 31 2 *")):
        with pytest.raises(ScheduleError):
            validate_schedule(kind, expr, now=NOON)


def test_api_refuses_an_invalid_schedule_with_422():
    app = create_app(settings=Settings(_env_file=None, environment="test", log_level="ERROR",
                                       local_auth_token=SecretStr(TOKEN), owner_id=OWNER))
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        bad = client.post("/scheduler/jobs", json={"name": "x", "schedule_type": "cron", "schedule_expr": "every day"})
        good = client.post("/scheduler/jobs", json={"name": "x", "schedule_type": "cron", "schedule_expr": "*/15 * * * *"})
        status = client.get("/status").json()
    assert bad.status_code == 422 and bad.json()["error"] == "invalid_schedule"
    assert good.status_code == 201 and good.json()["next_run_at"].endswith(("00Z", "00+00:00"))
    scheduler = next(item for item in status["subsystems"] if item["key"] == "scheduler")
    assert scheduler["state"] == "limited" and "runner off" in scheduler["checks"][0]


# ------------------------------------------------------------- due runs

def test_a_due_job_runs_once_and_missed_runs_collapse():
    service = make_service()
    job = service.create_job(OWNER, JobCreateRequest(name="hc", schedule_type="interval", schedule_expr="3600",
                                                     action_type="health_check"))
    make_due(service, job, NOON - timedelta(hours=10))  # ten missed intervals
    first = service.run_due(OWNER, now=NOON)
    second = service.run_due(OWNER, now=NOON)
    stored = service.get_job(OWNER, job.id)
    assert (first["jobs"], second["jobs"]) == (1, 0)
    assert stored.next_run_at == NOON + timedelta(hours=1) and stored.last_status == "success"


def test_concurrent_workers_run_a_due_job_once():
    service = make_service()
    runs = []
    original = service._run_job

    def counting(*args, **kwargs):
        runs.append(1)
        return original(*args, **kwargs)

    service._run_job = counting
    job = service.create_job(OWNER, JobCreateRequest(name="hc", schedule_type="interval", schedule_expr="3600",
                                                     action_type="health_check"))
    make_due(service, job, NOON - timedelta(minutes=1))
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        service.run_due(OWNER, now=NOON)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert runs == [1]


def test_a_one_time_job_runs_once_then_disables_itself():
    service = make_service()
    job = service.create_job(OWNER, JobCreateRequest(name="once", schedule_type="once", schedule_expr="60",
                                                     action_type="health_check"))
    make_due(service, job, NOON - timedelta(seconds=1))
    assert service.run_due(OWNER, now=NOON)["jobs"] == 1
    stored = service.get_job(OWNER, job.id)
    assert stored.enabled is False and stored.next_run_at is None
    assert service.run_due(OWNER, now=NOON + timedelta(days=1))["jobs"] == 0


# ------------------------------------------------------------- reminders and notifications

def test_due_reminders_are_delivered_once_and_never_throttled():
    assistant = AssistantService(MemoryAssistantStore())
    service = make_service(assistant_service=assistant)
    for title in ("Call the bank", "Water the plants"):
        assistant.create_reminder(OWNER, ReminderCreateRequest(title=title, remind_at=NOON - timedelta(minutes=5)))
    later = assistant.create_reminder(OWNER, ReminderCreateRequest(title="Later", remind_at=NOON + timedelta(hours=2)))
    assert service.run_due(OWNER, now=NOON)["reminders"] == 2
    assert service.run_due(OWNER, now=NOON)["reminders"] == 0
    titles = sorted(item.title for item in notifications(service))
    assert titles == ["Call the bank", "Water the plants"]  # both delivered: reminders bypass the throttle
    assert assistant.get_reminder(OWNER, later.id).status == ReminderStatus.PENDING


def test_quiet_hours_hold_notifications_until_they_end():
    assistant = AssistantService(MemoryAssistantStore())
    service = make_service(assistant_service=assistant, quiet_hours=(clock_time(22, 0), clock_time(7, 0)))
    night = datetime(2026, 10, 24, 23, 30, tzinfo=timezone.utc)
    assistant.create_reminder(OWNER, ReminderCreateRequest(title="Pills", remind_at=night - timedelta(minutes=1)))
    service.run_due(OWNER, now=night)
    urgent = service.dispatch_notification(OWNER, NotificationCreateRequest(
        category="security", title="Breach", message="Act now", priority="urgent"), now=night)
    held = [item for item in notifications(service) if item.title == "Pills"][0]
    assert held.delivered_at is None and urgent.delivered_at == night
    morning = datetime(2026, 10, 25, 7, 5, tzinfo=timezone.utc)
    assert service.run_due(OWNER, now=morning)["released"] == 1
    assert [item for item in notifications(service) if item.title == "Pills"][0].delivered_at == morning


def test_quiet_hours_use_the_configured_utc_offset():
    service = make_service(quiet_hours=(clock_time(22, 0), clock_time(7, 0)), utc_offset_minutes=330)  # UTC+05:30
    assert service.in_quiet_hours(datetime(2026, 10, 24, 17, 0, tzinfo=timezone.utc))  # 22:30 local
    assert not service.in_quiet_hours(datetime(2026, 10, 24, 6, 0, tzinfo=timezone.utc))  # 11:30 local


def test_the_throttle_survives_a_restart():
    store = MemorySchedulerStore()
    first = SchedulerService(store)
    request = NotificationCreateRequest(category="insight", title="Tip", message="Hydrate", priority="normal")
    assert first.dispatch_notification(OWNER, request, now=NOON) is not None
    restarted = SchedulerService(store)  # a new process: the in-memory throttle state is empty
    assert restarted.dispatch_notification(OWNER, request, now=NOON + timedelta(minutes=5)) is None
    assert restarted.dispatch_notification(OWNER, request, now=NOON + timedelta(minutes=16)) is not None


def test_invalid_quiet_hours_are_refused_at_startup():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, scheduler_quiet_hours="late")


# ------------------------------------------------------------- workflow jobs

def test_workflow_jobs_run_read_only_workflows_and_wait_for_approval_on_writes():
    assistant = AssistantService(MemoryAssistantStore())
    workflows = WorkflowService(MemoryWorkflowStore(), assistant_service=assistant)
    workflows.approval_service = memory_approvals()
    service = make_service(workflow_trigger=lambda owner, workflow_id: workflows.trigger_run(owner, workflow_id, {}))

    def define(action):
        return workflows.create_definition(OWNER, WorkflowDefinitionCreateRequest(name=action, dag_spec=DAGSpec(
            steps=[{"step_id": "s", "name": "s", "step_type": "assistant", "action": action}])))

    reads, writes = define("tasks.list"), define("tasks.create")
    ok = service.create_job(OWNER, JobCreateRequest(name="r", schedule_type="interval", schedule_expr="60",
                                                    action_type="workflow", payload={"workflow_id": str(reads.id)}))
    gated = service.create_job(OWNER, JobCreateRequest(name="w", schedule_type="interval", schedule_expr="60",
                                                       action_type="workflow", payload={"workflow_id": str(writes.id)}))
    ran = service.execute_job(OWNER, ok.id)
    waiting = service.execute_job(OWNER, gated.id)
    assert ran.status == "success" and ran.output["run_status"] == "completed"
    assert waiting.status == "failure" and "Approval required" in waiting.error
    assert assistant.list_tasks(OWNER).total == 0  # nothing was written
    assert any(item.category == "approval" for item in notifications(service))
    assert workflows.approval_service.list(OWNER, limit=10, state="pending").approvals


# ------------------------------------------------------------- runner

def test_runner_ticks_in_the_background_and_survives_a_failing_tick():
    calls = []

    class Flaky:
        def run_due(self, owner_id):
            calls.append(owner_id)
            if len(calls) == 1:
                raise RuntimeError("database down")
            return {"reminders": 0, "jobs": 0, "released": 0}

    runner = SchedulerRunner(Flaky(), OWNER, interval_seconds=0.05)
    runner.start()
    deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
    while len(calls) < 3 and datetime.now(timezone.utc) < deadline:
        threading.Event().wait(0.02)
    runner.stop()
    assert len(calls) >= 3 and runner.failed_ticks == 1 and not runner.running


def test_runner_is_off_by_default_and_runs_when_enabled():
    for enabled in (False, True):
        app = create_app(settings=Settings(_env_file=None, environment="test", log_level="ERROR",
                                           local_auth_token=SecretStr(TOKEN), owner_id=OWNER,
                                           scheduler_runner_enabled=enabled, scheduler_tick_seconds=5))
        with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
            running = app.state.scheduler_runner.running
            report = client.get("/status").json()
        scheduler = next(item for item in report["subsystems"] if item["key"] == "scheduler")
        assert running is enabled
        assert scheduler["checks"][0] == f"background runner {'running' if enabled else 'off'}"
        assert not app.state.scheduler_runner.running  # stopped on shutdown


# ------------------------------------------------------------- review fixes

from jarvis.scheduler.models import JobUpdateRequest  # noqa: E402


def _slow_job(service, release, started):
    original = service.health_probe

    def probe():
        started.set()
        release.wait(5)
        return original()

    service.health_probe = probe


@pytest.mark.parametrize("change", ["delete", "disable", "rename"])
def test_a_job_changed_during_its_run_keeps_the_change(change):
    service = make_service()
    job = service.create_job(OWNER, JobCreateRequest(name="hc", schedule_type="interval", schedule_expr="3600",
                                                     action_type="health_check"))
    make_due(service, job, NOON - timedelta(minutes=1))
    release, started = threading.Event(), threading.Event()
    _slow_job(service, release, started)
    worker = threading.Thread(target=service.run_due, args=(OWNER,), kwargs={"now": NOON})
    worker.start()
    assert started.wait(5)
    if change == "delete":
        service.delete_job(OWNER, job.id)
    elif change == "disable":
        service.update_job(OWNER, job.id, JobUpdateRequest(enabled=False))
    else:
        service.update_job(OWNER, job.id, JobUpdateRequest(name="renamed"))
    release.set()
    worker.join()
    stored = service.get_job(OWNER, job.id)
    if change == "delete":
        assert stored is None  # never re-created
    elif change == "disable":
        assert stored.enabled is False and stored.next_run_at is None
    else:
        assert stored.name == "renamed"  # the edit made during the run is kept


def test_a_slow_run_never_rewinds_the_schedule_for_another_worker():
    service = make_service()
    job = service.create_job(OWNER, JobCreateRequest(name="hc", schedule_type="interval", schedule_expr="60",
                                                     action_type="health_check"))
    make_due(service, job, NOON - timedelta(seconds=1))
    release, started = threading.Event(), threading.Event()
    _slow_job(service, release, started)
    slow = threading.Thread(target=service.run_due, args=(OWNER,), kwargs={"now": NOON})
    slow.start()
    assert started.wait(5)
    other = make_service()
    other.store = service.store
    later = NOON + timedelta(seconds=61)
    assert other.run_due(OWNER, now=later)["jobs"] == 1  # the next due run, claimed by another worker
    release.set()
    slow.join()
    assert service.get_job(OWNER, job.id).next_run_at == later + timedelta(seconds=60)  # not rewound


def test_a_full_scheduler_leaves_due_jobs_due():
    service = make_service()
    job = service.create_job(OWNER, JobCreateRequest(name="once", schedule_type="once", schedule_expr="60",
                                                     action_type="health_check"))
    make_due(service, job, NOON - timedelta(seconds=1))
    service._running_jobs.update({UUID(int=1), UUID(int=2)})  # two manual runs in flight
    assert service.run_due(OWNER, now=NOON)["jobs"] == 0
    stored = service.get_job(OWNER, job.id)
    assert stored.enabled and stored.next_run_at == NOON - timedelta(seconds=1)  # still due, not lost
    service._running_jobs.clear()
    assert service.run_due(OWNER, now=NOON)["jobs"] == 1


def test_jobs_left_running_by_a_crash_are_marked_failed_at_startup():
    store = MemorySchedulerStore()
    first = SchedulerService(store)
    job = first.create_job(OWNER, JobCreateRequest(name="hc", schedule_type="interval", schedule_expr="60",
                                                   action_type="health_check"))
    store.save_job(job.model_copy(update={"last_status": "running",
                                          "last_run_at": datetime.now(timezone.utc) - timedelta(hours=1)}))
    assert SchedulerService(store).recover_stale_runs(OWNER) == 1
    assert store.get_job(OWNER, job.id).last_status == "failure"


def test_quiet_hours_still_throttle_collapse_and_hide_held_items():
    service = make_service(quiet_hours=(clock_time(0, 0), clock_time(23, 59)))  # quiet almost all day
    night = datetime(2026, 10, 24, 23, 0, tzinfo=timezone.utc)
    for minute in range(20):
        service.dispatch_notification(OWNER, NotificationCreateRequest(
            category="approval", title="Workflow job needs your approval", message="m", priority="high"),
            now=night + timedelta(minutes=minute))
    held = [item for item in notifications(service) if item.category == "approval"]
    assert len(held) == 1 and held[0].delivered_at is None
    if service.in_quiet_hours(datetime.now(timezone.utc)):
        assert service.list_notifications(OWNER).total == 0  # held items are not shown yet


def test_held_notifications_are_released_on_listing_without_the_runner():
    service = make_service(quiet_hours=(clock_time(22, 0), clock_time(7, 0)))
    night = datetime(2026, 10, 24, 23, 0, tzinfo=timezone.utc)
    service.dispatch_notification(OWNER, NotificationCreateRequest(category="insight", title="Tip", message="m"),
                                  now=night)
    service.quiet_hours = None  # quiet hours are over
    assert service.list_notifications(OWNER).total == 1


def test_a_reminder_is_not_lost_when_storing_its_notification_fails():
    assistant = AssistantService(MemoryAssistantStore())
    service = make_service(assistant_service=assistant)
    reminder = assistant.create_reminder(OWNER, ReminderCreateRequest(title="Pay rent",
                                                                      remind_at=NOON - timedelta(minutes=1)))
    original = service.store.insert_notification_if_absent

    def failing(notification):
        raise RuntimeError("db down")

    service.store.insert_notification_if_absent = failing
    assert service.run_due(OWNER, now=NOON)["reminders"] == 0
    assert assistant.get_reminder(OWNER, reminder.id).status == ReminderStatus.PENDING  # still pending
    service.store.insert_notification_if_absent = original
    assert service.run_due(OWNER, now=NOON)["reminders"] == 1


def test_a_recurring_write_workflow_job_is_paused_instead_of_asking_every_tick():
    assistant = AssistantService(MemoryAssistantStore())
    workflows = WorkflowService(MemoryWorkflowStore(), assistant_service=assistant)
    workflows.approval_service = memory_approvals()
    service = make_service(workflow_trigger=lambda owner, workflow_id: workflows.trigger_run(owner, workflow_id, {}))
    writes = workflows.create_definition(OWNER, WorkflowDefinitionCreateRequest(name="w", dag_spec=DAGSpec(
        steps=[{"step_id": "s", "name": "s", "step_type": "assistant", "action": "tasks.create"}])))
    job = service.create_job(OWNER, JobCreateRequest(name="w", schedule_type="interval", schedule_expr="60",
                                                     action_type="workflow", payload={"workflow_id": str(writes.id)}))
    make_due(service, job, NOON - timedelta(seconds=1))
    for minute in range(5):
        service.run_due(OWNER, now=NOON + timedelta(minutes=minute))
    stored = service.get_job(OWNER, job.id)
    assert stored.enabled is False and stored.last_status == "failure"
    assert len([item for item in notifications(service) if item.category == "approval"]) == 1
    assert assistant.list_tasks(OWNER).total == 0


def test_cron_star_step_does_not_restrict_the_day_and_once_overflow_is_refused():
    assert CronExpression("0 9 */1 * 1").next_after(NOON) == datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
    for expr in ("inf", "nan", "1e20"):
        with pytest.raises(ScheduleError):
            validate_schedule("once", expr, now=NOON)


def test_a_job_with_an_old_invalid_schedule_can_still_be_disabled():
    service = make_service()
    job = service.create_job(OWNER, JobCreateRequest(name="old", schedule_type="interval", schedule_expr="60",
                                                     action_type="health_check"))
    service.store.save_job(job.model_copy(update={"schedule_expr": "10"}))  # stored before v4.5's rules
    assert service.update_job(OWNER, job.id, JobUpdateRequest(enabled=False)).enabled is False
