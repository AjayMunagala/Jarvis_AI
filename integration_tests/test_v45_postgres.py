"""v4.5 live PostgreSQL validation: once-only job claims, once-only reminders, held notifications, persisted throttle."""

import threading
from datetime import datetime, time as clock_time, timedelta, timezone

from test_postgres_integration import TEST_OWNER_ID


def services(url):
    from jarvis.assistant.service import AssistantService
    from jarvis.assistant.stores import PostgresAssistantStore
    from jarvis.scheduler.service import SchedulerService
    from jarvis.scheduler.stores import PostgresSchedulerStore

    scheduler = SchedulerService(PostgresSchedulerStore(url))
    scheduler.health_probe = lambda: {"database": "operational"}
    scheduler.assistant_service = AssistantService(PostgresAssistantStore(url))
    return scheduler


def test_a_due_job_is_claimed_once_across_concurrent_workers(isolated_postgres):
    from jarvis.scheduler.models import JobCreateRequest

    url = isolated_postgres.database_url
    first = services(url)
    job = first.create_job(TEST_OWNER_ID, JobCreateRequest(name="hc", schedule_type="interval", schedule_expr="3600",
                                                           action_type="health_check"))
    now = datetime.now(timezone.utc)
    first.store.save_job(job.model_copy(update={"next_run_at": now - timedelta(minutes=1)}))
    workers = [services(url) for _ in range(6)]  # separate instances, like separate processes
    results, barrier = [], threading.Barrier(len(workers))

    def run(worker):
        barrier.wait()
        results.append(worker.run_due(TEST_OWNER_ID, now=now)["jobs"])

    threads = [threading.Thread(target=run, args=(worker,)) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [0, 0, 0, 0, 0, 1]
    assert first.get_job(TEST_OWNER_ID, job.id).next_run_at > now


def test_reminders_hold_release_and_throttle_persist(isolated_postgres):
    from jarvis.assistant.models import ReminderCreateRequest
    from jarvis.scheduler.models import NotificationCreateRequest

    from uuid import uuid4

    owner = uuid4()  # the live database is shared by the session; a fresh owner isolates this test's data
    url = isolated_postgres.database_url
    scheduler = services(url)
    scheduler.quiet_hours = (clock_time(22, 0), clock_time(7, 0))
    night = datetime(2026, 10, 24, 23, 30, tzinfo=timezone.utc)
    scheduler.assistant_service.create_reminder(owner, ReminderCreateRequest(
        title="Pills", remind_at=night - timedelta(minutes=1)))
    assert scheduler.run_due(owner, now=night)["reminders"] == 1
    other = services(url)  # another worker with the same configuration
    other.quiet_hours = scheduler.quiet_hours
    assert other.run_due(owner, now=night)["reminders"] == 0  # already delivered
    items = scheduler.store.list_notifications(owner, unread_only=False, limit=50, offset=0)[0]
    assert [item.delivered_at for item in items if item.title == "Pills"] == [None]
    morning = datetime(2026, 10, 25, 7, 5, tzinfo=timezone.utc)
    assert scheduler.run_due(owner, now=morning)["released"] == 1
    request = NotificationCreateRequest(category="insight", title="Tip", message="Hydrate", priority="normal")
    assert scheduler.dispatch_notification(owner, request, now=morning) is not None
    restarted = services(url)  # fresh in-memory throttle; the last delivery is read from PostgreSQL
    assert restarted.dispatch_notification(owner, request, now=morning + timedelta(minutes=5)) is None
