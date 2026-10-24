"""Scheduler service coordinating background jobs, anti-fatigue alerts, and bounded execution (v3.5).

v4.5 bounded proactive runtime:
- Schedules are validated strictly (``jarvis.scheduler.cron``); cron
  expressions are evaluated for real instead of running hourly.
- ``run_due`` (called by ``SchedulerRunner`` when the background runner is
  enabled) claims each due job atomically, so a due run executes at most once
  even with several workers; missed runs after downtime collapse into one.
- Due assistant reminders are delivered once (pending -> triggered).
- Notifications during quiet hours are stored but held, and delivered when
  the quiet hours end; reminders are never dropped by the anti-fatigue throttle.
  The throttle reads the last delivery time from storage, so a restart does
  not reset it.
- Workflow jobs run through the workflow engine. A workflow with write steps
  still needs the owner's approval: the job records the approval request,
  notifies the owner and runs nothing.
"""

from datetime import datetime, time as clock_time, timedelta, timezone
import logging
import threading
from typing import Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from jarvis.scheduler.cron import ScheduleError, validate_schedule

from jarvis.scheduler.models import (
    JobCreateRequest,
    JobRunResult,
    JobUpdateRequest,
    Notification,
    NotificationCreateRequest,
    NotificationPage,
    ScheduledJob,
    ScheduledJobPage,
)
from jarvis.scheduler.stores import SchedulerStore
from jarvis.scheduler.throttle import AntiFatigueThrottler

logger = logging.getLogger(__name__)

MAX_CONCURRENT_JOBS = 2


def calculate_next_run(schedule_type: str, schedule_expr: str, base_time: datetime | None = None) -> datetime | None:
    """Next run time for a valid schedule; None for an invalid one (v4.5: no silent fallback intervals)."""
    try:
        return validate_schedule(schedule_type, schedule_expr, now=base_time or datetime.now(timezone.utc))
    except ScheduleError:
        return None


def parse_quiet_hours(text: str | None) -> tuple[clock_time, clock_time] | None:
    """``"HH:MM-HH:MM"`` (may cross midnight) or None; raises ValueError for a malformed value."""
    if not text:
        return None
    try:
        start_text, end_text = text.split("-")
        start, end = clock_time.fromisoformat(start_text.strip()), clock_time.fromisoformat(end_text.strip())
    except ValueError as exc:
        raise ValueError("Quiet hours must look like 22:00-07:00.") from exc
    if start == end:
        raise ValueError("Quiet hours must not start and end at the same time.")
    return start, end


class MaxConcurrencyError(RuntimeError):
    """Raised when the maximum number of concurrent running jobs is exceeded."""


class _PauseJob(RuntimeError):
    """The job cannot run unattended (for example it needs a single-use approval); it is disabled."""


class SchedulerService:
    def __init__(
        self,
        store: SchedulerStore,
        audit_service: Any = None,
        throttler: AntiFatigueThrottler | None = None,
    ) -> None:
        self.store = store
        self.audit_service = audit_service
        self.throttler = throttler or AntiFatigueThrottler()
        # v4.2: set by the application to StatusService.summary_map. Without it a health_check job fails
        # honestly instead of reporting a fixed "all_healthy: True".
        self.health_probe = None
        # v4.5: set by the application. ``workflow_trigger(owner_id, workflow_id)`` runs a workflow through the
        # workflow engine (raising ApprovalRequired when it has write steps); ``assistant_service`` supplies reminders.
        self.workflow_trigger: Callable[[UUID, UUID], Any] | None = None
        self.assistant_service: Any = None
        self.quiet_hours: tuple[clock_time, clock_time] | None = None
        self.utc_offset_minutes = 0
        self.max_jobs_per_tick = 5
        self._booted_at = datetime.now(timezone.utc)
        self._running_jobs_lock = threading.Lock()
        self._running_jobs: set[UUID] = set()

    def _audit(self, owner_id: UUID, event_type: str, subject: str, details: dict[str, Any]) -> None:
        if self.audit_service is not None:
            try:
                self.audit_service.record(
                    owner_id=owner_id,
                    category="scheduler",
                    event_type=event_type,
                    decision="allowed",
                    status="succeeded",
                    subject=subject[:200],
                    details=details,
                )
            except Exception as exc:
                logger.warning("Failed to record scheduler audit event: %s", exc)

    # ========================================================= Jobs

    def create_job(self, owner_id: UUID, req: JobCreateRequest) -> ScheduledJob:
        now = datetime.now(timezone.utc)
        first_run = validate_schedule(req.schedule_type, req.schedule_expr, now=now)  # ScheduleError -> HTTP 422
        next_run = first_run if req.enabled else None

        job = ScheduledJob(
            id=uuid4(),
            owner_id=owner_id,
            name=req.name,
            schedule_type=req.schedule_type,
            schedule_expr=req.schedule_expr,
            action_type=req.action_type,
            payload=req.payload,
            enabled=req.enabled,
            next_run_at=next_run,
            last_run_at=None,
            last_status="idle",
            created_at=now,
            updated_at=now,
        )
        self.store.save_job(job)
        self._audit(
            owner_id,
            "scheduler.job.create",
            f"scheduler:job:{job.id}",
            {"name": job.name, "type": job.schedule_type, "action": job.action_type},
        )
        return job

    def get_job(self, owner_id: UUID, job_id: UUID) -> ScheduledJob | None:
        return self.store.get_job(owner_id, job_id)

    def list_jobs(self, owner_id: UUID, limit: int = 50, offset: int = 0) -> ScheduledJobPage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        items, total = self.store.list_jobs(owner_id, limit=limit, offset=offset)
        return ScheduledJobPage(items=items, total=total)

    def update_job(self, owner_id: UUID, job_id: UUID, req: JobUpdateRequest) -> ScheduledJob | None:
        existing = self.store.get_job(owner_id, job_id)
        if not existing:
            return None

        now = datetime.now(timezone.utc)
        name = req.name if req.name is not None else existing.name
        schedule_type = req.schedule_type if req.schedule_type is not None else existing.schedule_type
        schedule_expr = req.schedule_expr if req.schedule_expr is not None else existing.schedule_expr
        action_type = req.action_type if req.action_type is not None else existing.action_type
        payload = req.payload if req.payload is not None else existing.payload
        enabled = req.enabled if req.enabled is not None else existing.enabled

        schedule_changed = (schedule_type != existing.schedule_type) or (schedule_expr != existing.schedule_expr) or (enabled != existing.enabled)
        next_run = existing.next_run_at
        if schedule_changed:
            if enabled:  # disabling never needs a valid schedule, so older jobs can always be switched off
                next_run = validate_schedule(schedule_type, schedule_expr, now=now)  # ScheduleError -> HTTP 422
            else:
                next_run = None

        updated = ScheduledJob(
            id=existing.id,
            owner_id=existing.owner_id,
            name=name,
            schedule_type=schedule_type,
            schedule_expr=schedule_expr,
            action_type=action_type,
            payload=payload,
            enabled=enabled,
            next_run_at=next_run,
            last_run_at=existing.last_run_at,
            last_status=existing.last_status,
            created_at=existing.created_at,
            updated_at=now,
        )
        self.store.save_job(updated)
        self._audit(
            owner_id,
            "scheduler.job.update",
            f"scheduler:job:{job_id}",
            {"name": updated.name, "enabled": updated.enabled},
        )
        return updated

    def delete_job(self, owner_id: UUID, job_id: UUID) -> bool:
        deleted = self.store.delete_job(owner_id, job_id)
        if deleted:
            self._audit(
                owner_id,
                "scheduler.job.delete",
                f"scheduler:job:{job_id}",
                {"deleted": True},
            )
        return deleted

    def execute_job(self, owner_id: UUID, job_id: UUID) -> JobRunResult:
        """Executes a scheduled job synchronously with hard concurrency and duration bounds."""
        job = self.store.get_job(owner_id, job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found.")
        return self._run_job(owner_id, job, trigger="manual")

    def _run_job(self, owner_id: UUID, job: ScheduledJob, *, trigger: str,
                 claimed: tuple[datetime | None, bool] | None = None) -> JobRunResult:
        """Run one job. ``claimed`` is the (next_run_at, enabled) a scheduled claim already stored (v4.5).

        The job row is only ever changed with ``record_run`` (compare-and-set on next_run_at): a run never
        re-creates a deleted job, re-enables a disabled one, or reverts an edit made while it ran."""
        job_id = job.id

        # Concurrency check
        with self._running_jobs_lock:
            if len(self._running_jobs) >= MAX_CONCURRENT_JOBS:
                raise MaxConcurrencyError(
                    f"Maximum concurrent running jobs limit ({MAX_CONCURRENT_JOBS}) reached."
                )
            self._running_jobs.add(job_id)

        started_at = datetime.now(timezone.utc)
        status = "success"
        error_msg = None
        output: dict[str, Any] = {}
        pause = False
        guard = claimed[0] if claimed is not None else job.next_run_at

        try:
            if claimed is None and not self.store.record_run(  # manual run: mark running (the claim already did)
                    owner_id, job_id, expected_next_run=guard, next_run=guard, enabled=job.enabled,
                    last_status="running", last_run_at=started_at, now=started_at):
                raise RuntimeError("The job changed or was deleted before it could start; nothing was run.")

            # Execution simulation / dispatch
            if job.action_type == "reminder":
                output = {"action": "reminder_dispatched", "title": job.payload.get("title", job.name)}
                # Proactively dispatch notification
                self.dispatch_notification(
                    owner_id,
                    NotificationCreateRequest(
                        category="reminder",
                        title=job.payload.get("title", job.name),
                        message=job.payload.get("message", f"Scheduled reminder for {job.name}"),
                        priority=job.payload.get("priority", "normal"),
                    ),
                )
            elif job.action_type == "workflow":
                output = self._run_workflow_job(owner_id, job)
            elif job.action_type == "health_check":
                if self.health_probe is None:
                    raise RuntimeError("No status source is connected; the health check could not run.")
                states = dict(self.health_probe())
                problems = sorted(key for key, state in states.items() if state == "unavailable")
                output = {"action": "health_check_completed", "all_healthy": not problems,
                          "unavailable": problems, "checked_subsystems": len(states)}
            else:
                output = {"action": "custom_job_completed", "payload": job.payload}

        except _PauseJob as exc:
            status, error_msg, pause = "failure", str(exc), True
        except Exception as exc:
            status = "failure"
            error_msg = str(exc)
            logger.exception("Failed to execute job %s: %s", job_id, exc)
        finally:
            with self._running_jobs_lock:
                self._running_jobs.discard(job_id)

        completed_at = datetime.now(timezone.utc)
        if claimed is not None:
            # v4.5: the claim already advanced next_run_at; keep what it stored.
            next_run, still_enabled = claimed
        else:
            # Advance next_run_at if recurring, else None
            next_run = (
                calculate_next_run(job.schedule_type, job.schedule_expr, base_time=completed_at)
                if job.schedule_type != "once"
                else None
            )
            still_enabled = job.enabled if job.schedule_type != "once" else False
        if pause:
            next_run, still_enabled = None, False
        recorded = self.store.record_run(owner_id, job_id, expected_next_run=guard, next_run=next_run,
                                         enabled=still_enabled, last_status=status, last_run_at=started_at,
                                         now=completed_at)

        self._audit(
            owner_id,
            "scheduler.job.execute",
            f"scheduler:job:{job.id}",
            {"status": status, "duration_ms": (completed_at - started_at).total_seconds() * 1000, "trigger": trigger,
             "paused": pause, "state_recorded": recorded},
        )

        return JobRunResult(
            job_id=job.id,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            output=output,
            error=error_msg,
        )

    def _run_workflow_job(self, owner_id: UUID, job: ScheduledJob) -> dict[str, Any]:
        from jarvis.audit.service import ApprovalRequired

        if self.workflow_trigger is None:
            raise RuntimeError("The workflow engine is not connected; nothing was executed.")
        try:
            workflow_id = UUID(str(job.payload.get("workflow_id", "")))
        except ValueError as exc:
            raise RuntimeError("The job payload needs a valid workflow_id; nothing was executed.") from exc
        try:
            detail = self.workflow_trigger(owner_id, workflow_id)
        except ApprovalRequired as required:
            # A single-use approval cannot cover a recurring unattended job, so the job is paused (disabled) and the
            # owner is told once, instead of a new request and notification on every tick.
            approval_id = required.request.approval_id
            self.dispatch_notification(owner_id, NotificationCreateRequest(
                category="approval", title=f"Workflow job '{job.name}' is paused: it needs your approval",
                message=f"The workflow writes data, so it cannot run unattended. Approve {approval_id} and run the "
                        "workflow yourself with that approval ID. Nothing was executed.",
                priority="normal"))
            raise _PauseJob(f"Approval required ({approval_id}); the job was paused and nothing was executed.") from None
        status = getattr(getattr(detail, "run", None), "status", None)
        output = {"action": "workflow_run", "run_id": str(detail.run.id), "run_status": str(status)}
        if str(status) not in ("completed", "running"):  # "running": started in the background
            raise RuntimeError(f"Workflow run {detail.run.id} ended {status}: {detail.run.error_message}")
        return output

    # ========================================================= v4.5 background runtime

    def run_due(self, owner_id: UUID, now: datetime | None = None) -> dict[str, int]:
        """One runner tick: deliver due reminders, run due jobs once each, release held notifications.

        Delivery is at most once: a run claimed by a process that then crashes is not retried; startup recovery
        marks such a job as failed (``recover_stale_runs``)."""
        now = now or datetime.now(timezone.utc)
        summary = {"reminders": self.deliver_due_reminders(owner_id, now), "jobs": 0, "released": 0}
        for job in self.store.get_due_jobs(cutoff=now, limit=self.max_jobs_per_tick):
            if job.owner_id != owner_id or job.next_run_at is None:
                continue
            with self._running_jobs_lock:
                if len(self._running_jobs) >= MAX_CONCURRENT_JOBS:
                    break  # leave it due: it is claimed on a later tick instead of being lost (finding 2)
            # Missed runs collapse into one: the next run is computed from now, not from the missed time.
            next_run = (calculate_next_run(job.schedule_type, job.schedule_expr, base_time=now)
                        if job.schedule_type != "once" else None)
            enabled = job.schedule_type != "once" and next_run is not None
            if not self.store.claim_job(owner_id, job.id, expected_next_run=job.next_run_at, next_run=next_run,
                                        enabled=enabled, now=now):
                continue  # another worker claimed this due run
            if job.schedule_type != "once" and next_run is None:
                self._audit(owner_id, "scheduler.job.disabled_invalid_schedule", f"scheduler:job:{job.id}",
                            {"schedule_type": job.schedule_type})
            current = self.store.get_job(owner_id, job.id) or job  # the latest payload, not the pre-claim copy
            try:
                self._run_job(owner_id, current, trigger="scheduled", claimed=(next_run, enabled))
            except MaxConcurrencyError:
                self.store.record_run(owner_id, job.id, expected_next_run=next_run, next_run=next_run,
                                      enabled=enabled, last_status="failure", last_run_at=now, now=now)
                logger.warning("Scheduled job %s skipped: concurrency limit reached", job.id)
                continue
            summary["jobs"] += 1
        if not self.in_quiet_hours(now):
            summary["released"] = self.store.release_held_notifications(owner_id, now)
        return summary

    def recover_stale_runs(self, owner_id: UUID) -> int:
        """Mark jobs left 'running' by a previous process as failed (v4.5 review finding 3)."""
        count = self.store.fail_stale_running(owner_id, before=self._booted_at, now=datetime.now(timezone.utc))
        if count:
            self._audit(owner_id, "scheduler.job.recover", "scheduler:jobs", {"interrupted_jobs": count})
        return count

    def deliver_due_reminders(self, owner_id: UUID, now: datetime) -> int:
        """Deliver each due reminder once. The notification is stored first, under an ID derived from the
        reminder, then the reminder is marked triggered; a failure leaves it pending for the next tick."""
        if self.assistant_service is None:
            return 0
        from jarvis.assistant.models import ReminderStatus

        delivered = 0
        due, _ = self.assistant_service.store.list_reminders(owner_id, status=ReminderStatus.PENDING, due_before=now,
                                                             limit=50, offset=0)
        for reminder in due:
            try:
                held = self.in_quiet_hours(now)
                notification = Notification(
                    id=uuid5(NAMESPACE_URL, f"jarvis:reminder:{reminder.id}"), owner_id=owner_id, category="reminder",
                    title=reminder.title, message=f"Reminder due at {reminder.remind_at.isoformat(timespec='minutes')}.",
                    priority="normal", read=False, delivered_at=None if held else now, created_at=now)
                self.store.insert_notification_if_absent(notification)
                if self.assistant_service.store.trigger_reminder(owner_id, reminder.id, now) is None:
                    continue  # delivered by another worker (same notification ID, so no duplicate)
                self._audit(owner_id, "scheduler.reminder.deliver", f"assistant:reminder:{reminder.id}",
                            {"held": held})
                delivered += 1
            except Exception as exc:  # noqa: BLE001 - one reminder must not stop the tick; it stays pending
                logger.warning("Reminder %s could not be delivered: %s", reminder.id, type(exc).__name__)
        return delivered

    def in_quiet_hours(self, now: datetime) -> bool:
        if self.quiet_hours is None:
            return False
        start, end = self.quiet_hours
        local = (now + timedelta(minutes=self.utc_offset_minutes)).time()
        return (start <= local < end) if start < end else (local >= start or local < end)

    # ========================================================= Notifications

    def dispatch_notification(
        self, owner_id: UUID, req: NotificationCreateRequest, now: datetime | None = None
    ) -> Notification | None:
        """Dispatches a notification, applying anti-fatigue throttling and quiet hours (v4.5)."""
        now = now or datetime.now(timezone.utc)
        held = self.in_quiet_hours(now) and req.priority != "urgent"
        if held and self.store.held_notification_exists(owner_id, req.category, req.title):
            return None  # the same notification is already waiting for the end of quiet hours
        if req.category != "reminder":  # reminders were asked for: never dropped
            allowed, reason = self.throttler.should_allow(
                owner_id=owner_id,
                category=req.category,
                priority=req.priority,
                now=now,
                last_dispatched=self._persisted_last_dispatch(owner_id, req.category),
            )
            if not allowed:
                logger.info("Notification throttled for owner %s: %s", owner_id, reason)
                self._audit(owner_id, "scheduler.notification.throttled", f"scheduler:notif:{req.category}",
                            {"category": req.category, "priority": req.priority})
                return None

        notification = Notification(
            id=uuid4(),
            owner_id=owner_id,
            category=req.category,
            title=req.title,
            message=req.message,
            priority=req.priority,
            read=False,
            delivered_at=None if held else now,
            created_at=now,
        )
        self.store.save_notification(notification)
        self._audit(
            owner_id,
            "scheduler.notification.hold" if held else "scheduler.notification.send",
            f"scheduler:notif:{notification.id}",
            {"category": notification.category, "priority": notification.priority, "held": held},
        )
        return notification

    def _persisted_last_dispatch(self, owner_id: UUID, category: str) -> datetime | None:
        try:
            return self.store.latest_delivered_at(owner_id, category)
        except Exception as exc:  # noqa: BLE001 - fall back to the in-memory throttle state
            logger.warning("Throttle state could not be read: %s", type(exc).__name__)
            return None

    def get_notification(self, owner_id: UUID, notif_id: UUID) -> Notification | None:
        return self.store.get_notification(owner_id, notif_id)

    def list_notifications(
        self,
        owner_id: UUID,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> NotificationPage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        now = datetime.now(timezone.utc)
        if not self.in_quiet_hours(now):  # held notifications appear once quiet hours end, even without the runner
            self.store.release_held_notifications(owner_id, now)
        items, total = self.store.list_notifications(
            owner_id, unread_only=unread_only, limit=limit, offset=offset, include_held=False
        )
        return NotificationPage(items=items, total=total)

    def mark_notification_read(self, owner_id: UUID, notif_id: UUID) -> bool:
        marked = self.store.mark_notification_read(owner_id, notif_id)
        if marked:
            self._audit(
                owner_id,
                "scheduler.notification.read",
                f"scheduler:notif:{notif_id}",
                {"read": True},
            )
        return marked
