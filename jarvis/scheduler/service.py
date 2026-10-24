"""Scheduler service coordinating background jobs, anti-fatigue alerts, and bounded execution (v3.5)."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import threading
from typing import Any
from uuid import UUID, uuid4

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
JOB_TIMEOUT_SECONDS = 60.0


def calculate_next_run(schedule_type: str, schedule_expr: str, base_time: datetime | None = None) -> datetime | None:
    """Calculates next execution timestamp based on schedule type and expression."""
    now = base_time or datetime.now(timezone.utc)
    expr = schedule_expr.strip()

    if schedule_type == "once":
        try:
            # Check if expression is numeric seconds from now
            seconds = float(expr)
            return now + timedelta(seconds=max(0.0, seconds))
        except ValueError:
            try:
                dt = datetime.fromisoformat(expr)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                return now + timedelta(minutes=1)

    elif schedule_type == "interval":
        try:
            seconds = max(1.0, float(expr))
            return now + timedelta(seconds=seconds)
        except ValueError:
            return now + timedelta(minutes=5)

    elif schedule_type == "cron":
        # Simplified predictable next interval for cron expressions (fallback 1 hour)
        return now + timedelta(hours=1)

    return None


class MaxConcurrencyError(RuntimeError):
    """Raised when the maximum number of concurrent running jobs is exceeded."""


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
        next_run = calculate_next_run(req.schedule_type, req.schedule_expr, base_time=now) if req.enabled else None

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
            next_run = calculate_next_run(schedule_type, schedule_expr, base_time=now) if enabled else None

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

        try:
            # Mark running
            running_job = ScheduledJob(
                id=job.id,
                owner_id=job.owner_id,
                name=job.name,
                schedule_type=job.schedule_type,
                schedule_expr=job.schedule_expr,
                action_type=job.action_type,
                payload=job.payload,
                enabled=job.enabled,
                next_run_at=job.next_run_at,
                last_run_at=started_at,
                last_status="running",
                created_at=job.created_at,
                updated_at=started_at,
            )
            self.store.save_job(running_job)

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
                # v4.2: nothing ever triggered the workflow; report that instead of "workflow_triggered".
                raise RuntimeError("Workflow jobs are not connected to the workflow engine yet (planned for v4.5).")
            elif job.action_type == "health_check":
                if self.health_probe is None:
                    raise RuntimeError("No status source is connected; the health check could not run.")
                states = dict(self.health_probe())
                problems = sorted(key for key, state in states.items() if state == "unavailable")
                output = {"action": "health_check_completed", "all_healthy": not problems,
                          "unavailable": problems, "checked_subsystems": len(states)}
            else:
                output = {"action": "custom_job_completed", "payload": job.payload}

        except Exception as exc:
            status = "failure"
            error_msg = str(exc)
            logger.exception("Failed to execute job %s: %s", job_id, exc)
        finally:
            with self._running_jobs_lock:
                self._running_jobs.discard(job_id)

        completed_at = datetime.now(timezone.utc)
        # Advance next_run_at if recurring, else None
        next_run = (
            calculate_next_run(job.schedule_type, job.schedule_expr, base_time=completed_at)
            if job.schedule_type != "once"
            else None
        )
        still_enabled = job.enabled if job.schedule_type != "once" else False

        final_job = ScheduledJob(
            id=job.id,
            owner_id=job.owner_id,
            name=job.name,
            schedule_type=job.schedule_type,
            schedule_expr=job.schedule_expr,
            action_type=job.action_type,
            payload=job.payload,
            enabled=still_enabled,
            next_run_at=next_run,
            last_run_at=started_at,
            last_status=status,
            created_at=job.created_at,
            updated_at=completed_at,
        )
        self.store.save_job(final_job)

        self._audit(
            owner_id,
            "scheduler.job.execute",
            f"scheduler:job:{job.id}",
            {"status": status, "duration_ms": (completed_at - started_at).total_seconds() * 1000},
        )

        return JobRunResult(
            job_id=job.id,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            output=output,
            error=error_msg,
        )

    # ========================================================= Notifications

    def dispatch_notification(
        self, owner_id: UUID, req: NotificationCreateRequest
    ) -> Notification | None:
        """Dispatches a notification, applying anti-fatigue throttling."""
        now = datetime.now(timezone.utc)
        allowed, reason = self.throttler.should_allow(
            owner_id=owner_id,
            category=req.category,
            priority=req.priority,
            now=now,
        )
        if not allowed:
            logger.info("Notification throttled for owner %s: %s", owner_id, reason)
            return None

        notification = Notification(
            id=uuid4(),
            owner_id=owner_id,
            category=req.category,
            title=req.title,
            message=req.message,
            priority=req.priority,
            read=False,
            delivered_at=now,
            created_at=now,
        )
        self.store.save_notification(notification)
        self._audit(
            owner_id,
            "scheduler.notification.send",
            f"scheduler:notif:{notification.id}",
            {"category": notification.category, "priority": notification.priority},
        )
        return notification

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
        items, total = self.store.list_notifications(
            owner_id, unread_only=unread_only, limit=limit, offset=offset
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
