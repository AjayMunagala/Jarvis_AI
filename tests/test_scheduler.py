"""Unit tests for the v3.5 Proactive Assistance & Scheduling subsystem."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest

from jarvis.scheduler.models import (
    JobCreateRequest,
    JobUpdateRequest,
    NotificationCreateRequest,
)
from jarvis.scheduler.service import (
    MaxConcurrencyError,
    SchedulerService,
    calculate_next_run,
)
from jarvis.scheduler.stores import MemorySchedulerStore
from jarvis.scheduler.throttle import AntiFatigueThrottler

TEST_OWNER = uuid4()


def test_calculate_next_run():
    base = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)

    # interval
    next_run = calculate_next_run("interval", "300", base_time=base)
    assert next_run == base + timedelta(seconds=300)

    # once relative
    next_run = calculate_next_run("once", "60", base_time=base)
    assert next_run == base + timedelta(seconds=60)

    # once ISO
    iso_time = "2026-10-18T15:30:00+00:00"
    next_run = calculate_next_run("once", iso_time, base_time=base)
    assert next_run == datetime.fromisoformat(iso_time)

    # cron (v4.5: evaluated for real; previously every expression ran hourly)
    next_run = calculate_next_run("cron", "0 9 * * *", base_time=base)
    assert next_run == datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)

    # invalid expressions have no next run (v4.5: no silent fallback interval)
    assert calculate_next_run("interval", "soon", base_time=base) is None
    assert calculate_next_run("cron", "99 * * * *", base_time=base) is None


def test_anti_fatigue_throttling():
    throttler = AntiFatigueThrottler(throttle_seconds=900)  # 15 min
    base = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)

    # 1st normal notification allowed
    ok, _ = throttler.should_allow(TEST_OWNER, "health", priority="normal", now=base)
    assert ok is True

    # 2nd normal notification 5 minutes later throttled
    ok, reason = throttler.should_allow(TEST_OWNER, "health", priority="normal", now=base + timedelta(minutes=5))
    assert ok is False
    assert "throttled" in reason.lower()

    # Different category allowed
    ok, _ = throttler.should_allow(TEST_OWNER, "reminders", priority="normal", now=base + timedelta(minutes=5))
    assert ok is True

    # Urgent notification bypasses quiet window
    ok, _ = throttler.should_allow(TEST_OWNER, "health", priority="urgent", now=base + timedelta(minutes=6))
    assert ok is True

    # High notification bypasses quiet window
    ok, _ = throttler.should_allow(TEST_OWNER, "health", priority="high", now=base + timedelta(minutes=7))
    assert ok is True

    # After 16 minutes, normal notification allowed again
    ok, _ = throttler.should_allow(TEST_OWNER, "health", priority="normal", now=base + timedelta(minutes=24))
    assert ok is True


def test_scheduler_store_and_service_lifecycle():
    store = MemorySchedulerStore()
    throttler = AntiFatigueThrottler(throttle_seconds=60)
    service = SchedulerService(store, throttler=throttler)

    # Create job
    job = service.create_job(
        TEST_OWNER,
        JobCreateRequest(
            name="Daily System Scan",
            schedule_type="interval",
            schedule_expr="3600",
            action_type="health_check",
            payload={"deep": True},
        ),
    )
    assert job.name == "Daily System Scan"
    assert job.next_run_at is not None

    # List jobs
    page = service.list_jobs(TEST_OWNER)
    assert page.total == 1
    assert page.items[0].id == job.id

    # Update job
    updated = service.update_job(
        TEST_OWNER,
        job.id,
        JobUpdateRequest(schedule_expr="7200", enabled=True),
    )
    assert updated is not None
    assert updated.schedule_expr == "7200"

    # Execute job. v4.2: without a connected status source the health check fails honestly
    # (it used to report a fixed all_healthy=True).
    unconnected = service.execute_job(TEST_OWNER, job.id)
    assert unconnected.status == "failure" and "No status source" in unconnected.error
    service.health_probe = lambda: {"database": "operational", "scheduler": "limited", "provider": "unavailable"}
    result = service.execute_job(TEST_OWNER, job.id)
    assert result.status == "success"
    assert result.output["all_healthy"] is False and result.output["unavailable"] == ["provider"]

    # Due jobs query
    future = datetime.now(timezone.utc) + timedelta(days=1)
    due = store.get_due_jobs(cutoff=future)
    assert len(due) == 1

    # Delete job
    assert service.delete_job(TEST_OWNER, job.id) is True
    assert service.get_job(TEST_OWNER, job.id) is None


def test_scheduler_concurrency_ceiling():
    store = MemorySchedulerStore()
    service = SchedulerService(store)

    # Manually simulate 2 currently running jobs
    j1 = service.create_job(TEST_OWNER, JobCreateRequest(name="J1", schedule_type="interval", schedule_expr="60"))
    j2 = service.create_job(TEST_OWNER, JobCreateRequest(name="J2", schedule_type="interval", schedule_expr="60"))
    j3 = service.create_job(TEST_OWNER, JobCreateRequest(name="J3", schedule_type="interval", schedule_expr="60"))

    # Acquire lock and populate running jobs set
    with service._running_jobs_lock:
        service._running_jobs.add(j1.id)
        service._running_jobs.add(j2.id)

    # Attempting to execute 3rd job must raise MaxConcurrencyError
    with pytest.raises(MaxConcurrencyError, match="Maximum concurrent running jobs limit"):
        service.execute_job(TEST_OWNER, j3.id)

    # Free one job slot
    with service._running_jobs_lock:
        service._running_jobs.remove(j1.id)

    # Execution now succeeds
    res = service.execute_job(TEST_OWNER, j3.id)
    assert res.status == "success"


def test_notification_dispatch_and_read():
    store = MemorySchedulerStore()
    service = SchedulerService(store)

    notif = service.dispatch_notification(
        TEST_OWNER,
        NotificationCreateRequest(
            category="assistant",
            title="Meeting in 10 minutes",
            message="Join design review link.",
            priority="normal",
        ),
    )
    assert notif is not None
    assert notif.read is False

    # List notifications
    page = service.list_notifications(TEST_OWNER, unread_only=True)
    assert page.total == 1
    assert page.items[0].id == notif.id

    # Mark read
    assert service.mark_notification_read(TEST_OWNER, notif.id) is True

    # Unread list is now 0
    page_unread = service.list_notifications(TEST_OWNER, unread_only=True)
    assert page_unread.total == 0

    # Total notifications still 1
    page_all = service.list_notifications(TEST_OWNER, unread_only=False)
    assert page_all.total == 1
    assert page_all.items[0].read is True
