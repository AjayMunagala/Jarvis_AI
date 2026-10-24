"""Persistence layer for scheduler jobs and notifications (v3.5)."""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
import threading
from typing import Any
from uuid import UUID

from jarvis.scheduler.models import Notification, ScheduledJob


class SchedulerStore(ABC):
    @abstractmethod
    def save_job(self, job: ScheduledJob) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_job(self, owner_id: UUID, job_id: UUID) -> ScheduledJob | None:
        raise NotImplementedError

    @abstractmethod
    def list_jobs(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[list[ScheduledJob], int]:
        raise NotImplementedError

    @abstractmethod
    def delete_job(self, owner_id: UUID, job_id: UUID) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_due_jobs(self, cutoff: datetime, limit: int = 20) -> list[ScheduledJob]:
        raise NotImplementedError

    @abstractmethod
    def save_notification(self, notification: Notification) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_notification(self, owner_id: UUID, notif_id: UUID) -> Notification | None:
        raise NotImplementedError

    @abstractmethod
    def list_notifications(
        self,
        owner_id: UUID,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
        include_held: bool = True,
    ) -> tuple[list[Notification], int]:
        raise NotImplementedError

    @abstractmethod
    def mark_notification_read(self, owner_id: UUID, notif_id: UUID) -> bool:
        raise NotImplementedError

    # v4.5 background runtime ------------------------------------------------

    @abstractmethod
    def claim_job(self, owner_id: UUID, job_id: UUID, *, expected_next_run: datetime,
                  next_run: datetime | None, enabled: bool, now: datetime) -> bool:
        """Atomically move a due job to its next run time; only one caller can claim a given due run."""
        raise NotImplementedError

    @abstractmethod
    def record_run(self, owner_id: UUID, job_id: UUID, *, expected_next_run: datetime | None,
                   next_run: datetime | None, enabled: bool, last_status: str, last_run_at: datetime,
                   now: datetime) -> bool:
        """Record a run's state only if the job still exists and next_run_at is unchanged (compare-and-set).

        Never re-creates a deleted job and never reverts a concurrent edit (finding 1 of the v4.5 review)."""
        raise NotImplementedError

    @abstractmethod
    def fail_stale_running(self, owner_id: UUID, before: datetime, now: datetime) -> int:
        """Mark jobs left 'running' by a previous process as failed."""
        raise NotImplementedError

    @abstractmethod
    def insert_notification_if_absent(self, notification: Notification) -> bool:
        raise NotImplementedError

    @abstractmethod
    def held_notification_exists(self, owner_id: UUID, category: str, title: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def latest_delivered_at(self, owner_id: UUID, category: str) -> datetime | None:
        """When the last notification of this category was delivered (persisted throttle state)."""
        raise NotImplementedError

    @abstractmethod
    def release_held_notifications(self, owner_id: UUID, now: datetime) -> int:
        """Deliver notifications held during quiet hours."""
        raise NotImplementedError


class MemorySchedulerStore(SchedulerStore):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[UUID, ScheduledJob] = {}
        self._notifications: dict[UUID, Notification] = {}

    def save_job(self, job: ScheduledJob) -> None:
        with self._lock:
            self._jobs[job.id] = job

    def get_job(self, owner_id: UUID, job_id: UUID) -> ScheduledJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.owner_id == owner_id:
                return job
            return None

    def list_jobs(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[list[ScheduledJob], int]:
        with self._lock:
            matching = [
                j for j in self._jobs.values()
                if j.owner_id == owner_id
            ]
            matching.sort(key=lambda j: j.created_at, reverse=True)
            total = len(matching)
            return matching[offset : offset + limit], total

    def delete_job(self, owner_id: UUID, job_id: UUID) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.owner_id == owner_id:
                del self._jobs[job_id]
                return True
            return False

    def get_due_jobs(self, cutoff: datetime, limit: int = 20) -> list[ScheduledJob]:
        with self._lock:
            due = [
                j for j in self._jobs.values()
                if j.enabled and j.next_run_at is not None and j.next_run_at <= cutoff
            ]
            due.sort(key=lambda j: j.next_run_at or cutoff)
            return due[:limit]

    def claim_job(self, owner_id, job_id, *, expected_next_run, next_run, enabled, now):
        with self._lock:
            job = self._jobs.get(job_id)
            if (job is None or job.owner_id != owner_id or not job.enabled or job.next_run_at != expected_next_run):
                return False
            self._jobs[job_id] = job.model_copy(update={
                "next_run_at": next_run, "enabled": enabled, "last_run_at": now, "last_status": "running",
                "updated_at": now})
            return True

    def record_run(self, owner_id, job_id, *, expected_next_run, next_run, enabled, last_status, last_run_at, now):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.owner_id != owner_id or job.next_run_at != expected_next_run:
                return False
            self._jobs[job_id] = job.model_copy(update={
                "next_run_at": next_run, "enabled": enabled, "last_status": last_status, "last_run_at": last_run_at,
                "updated_at": now})
            return True

    def fail_stale_running(self, owner_id, before, now):
        with self._lock:
            stale = [job for job in self._jobs.values() if job.owner_id == owner_id and job.last_status == "running"
                     and (job.last_run_at is None or job.last_run_at < before)]
            for job in stale:
                self._jobs[job.id] = job.model_copy(update={"last_status": "failure", "updated_at": now})
        return len(stale)

    def insert_notification_if_absent(self, notification):
        with self._lock:
            if notification.id in self._notifications:
                return False
            self._notifications[notification.id] = notification
            return True

    def held_notification_exists(self, owner_id, category, title):
        with self._lock:
            return any(n.owner_id == owner_id and n.category == category and n.title == title
                       and n.delivered_at is None for n in self._notifications.values())

    def latest_delivered_at(self, owner_id, category):
        with self._lock:
            times = [n.delivered_at for n in self._notifications.values()
                     if n.owner_id == owner_id and n.category == category and n.delivered_at is not None]
        return max(times, default=None)

    def release_held_notifications(self, owner_id, now):
        with self._lock:
            held = [n for n in self._notifications.values() if n.owner_id == owner_id and n.delivered_at is None]
            for n in held:
                self._notifications[n.id] = n.model_copy(update={"delivered_at": now})
        return len(held)

    def save_notification(self, notification: Notification) -> None:
        with self._lock:
            self._notifications[notification.id] = notification

    def get_notification(self, owner_id: UUID, notif_id: UUID) -> Notification | None:
        with self._lock:
            n = self._notifications.get(notif_id)
            if n and n.owner_id == owner_id:
                return n
            return None

    def list_notifications(
        self,
        owner_id: UUID,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
        include_held: bool = True,
    ) -> tuple[list[Notification], int]:
        with self._lock:
            matching = [
                n for n in self._notifications.values()
                if n.owner_id == owner_id and (not unread_only or not n.read)
                and (include_held or n.delivered_at is not None)
            ]
            matching.sort(key=lambda n: n.created_at, reverse=True)
            total = len(matching)
            return matching[offset : offset + limit], total

    def mark_notification_read(self, owner_id: UUID, notif_id: UUID) -> bool:
        with self._lock:
            n = self._notifications.get(notif_id)
            if n and n.owner_id == owner_id:
                self._notifications[notif_id] = Notification(
                    id=n.id,
                    owner_id=n.owner_id,
                    category=n.category,
                    title=n.title,
                    message=n.message,
                    priority=n.priority,
                    read=True,
                    delivered_at=n.delivered_at,
                    created_at=n.created_at,
                )
                return True
            return False


class PostgresSchedulerStore(SchedulerStore):
    def __init__(
        self,
        db_url: str,
        connect_timeout_seconds: float = 5.0,
        operation_timeout_seconds: float = 10.0,
        **kwargs: Any,
    ) -> None:
        self.db_url = db_url
        self.connect_timeout_seconds = connect_timeout_seconds
        self.operation_timeout_seconds = operation_timeout_seconds

    def _get_connection(self) -> Any:
        import psycopg
        # v4.5: a statement timeout, so a hung query cannot block the background runner indefinitely.
        return psycopg.connect(
            self.db_url,
            connect_timeout=int(self.connect_timeout_seconds),
            options=f"-c statement_timeout={int(self.operation_timeout_seconds * 1000)}",
        )

    def save_job(self, job: ScheduledJob) -> None:
        query = """
            INSERT INTO jarvis_app.scheduled_jobs (
                id, owner_id, name, schedule_type, schedule_expr, action_type,
                payload, enabled, next_run_at, last_run_at, last_status,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                schedule_type = EXCLUDED.schedule_type,
                schedule_expr = EXCLUDED.schedule_expr,
                action_type = EXCLUDED.action_type,
                payload = EXCLUDED.payload,
                enabled = EXCLUDED.enabled,
                next_run_at = EXCLUDED.next_run_at,
                last_run_at = EXCLUDED.last_run_at,
                last_status = EXCLUDED.last_status,
                updated_at = EXCLUDED.updated_at;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    query,
                    (
                        job.id,
                        job.owner_id,
                        job.name,
                        job.schedule_type,
                        job.schedule_expr,
                        job.action_type,
                        json.dumps(job.payload),
                        job.enabled,
                        job.next_run_at,
                        job.last_run_at,
                        job.last_status,
                        job.created_at,
                        job.updated_at,
                    ),
                )
            conn.commit()

    def get_job(self, owner_id: UUID, job_id: UUID) -> ScheduledJob | None:
        query = """
            SELECT id, owner_id, name, schedule_type, schedule_expr, action_type,
                   payload, enabled, next_run_at, last_run_at, last_status,
                   created_at, updated_at
            FROM jarvis_app.scheduled_jobs
            WHERE id = %s AND owner_id = %s;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (job_id, owner_id))
                row = cur.fetchone()
                if not row:
                    return None
                return ScheduledJob(
                    id=row[0],
                    owner_id=row[1],
                    name=row[2],
                    schedule_type=row[3],
                    schedule_expr=row[4],
                    action_type=row[5],
                    payload=row[6] if isinstance(row[6], dict) else json.loads(row[6]),
                    enabled=row[7],
                    next_run_at=row[8],
                    last_run_at=row[9],
                    last_status=row[10],
                    created_at=row[11],
                    updated_at=row[12],
                )

    def list_jobs(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[list[ScheduledJob], int]:
        count_q = "SELECT COUNT(*) FROM jarvis_app.scheduled_jobs WHERE owner_id = %s;"
        query = """
            SELECT id, owner_id, name, schedule_type, schedule_expr, action_type,
                   payload, enabled, next_run_at, last_run_at, last_status,
                   created_at, updated_at
            FROM jarvis_app.scheduled_jobs
            WHERE owner_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(count_q, (owner_id,))
                total = cur.fetchone()[0]
                cur.execute(query, (owner_id, limit, offset))
                rows = cur.fetchall()
                jobs = [
                    ScheduledJob(
                        id=r[0],
                        owner_id=r[1],
                        name=r[2],
                        schedule_type=r[3],
                        schedule_expr=r[4],
                        action_type=r[5],
                        payload=r[6] if isinstance(r[6], dict) else json.loads(r[6]),
                        enabled=r[7],
                        next_run_at=r[8],
                        last_run_at=r[9],
                        last_status=r[10],
                        created_at=r[11],
                        updated_at=r[12],
                    )
                    for r in rows
                ]
                return jobs, total

    def delete_job(self, owner_id: UUID, job_id: UUID) -> bool:
        query = "DELETE FROM jarvis_app.scheduled_jobs WHERE id = %s AND owner_id = %s;"
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (job_id, owner_id))
                deleted = cur.rowcount > 0
            conn.commit()
            return deleted

    def get_due_jobs(self, cutoff: datetime, limit: int = 20) -> list[ScheduledJob]:
        query = """
            SELECT id, owner_id, name, schedule_type, schedule_expr, action_type,
                   payload, enabled, next_run_at, last_run_at, last_status,
                   created_at, updated_at
            FROM jarvis_app.scheduled_jobs
            WHERE enabled = true AND next_run_at IS NOT NULL AND next_run_at <= %s
            ORDER BY next_run_at ASC
            LIMIT %s;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (cutoff, limit))
                rows = cur.fetchall()
                return [
                    ScheduledJob(
                        id=r[0],
                        owner_id=r[1],
                        name=r[2],
                        schedule_type=r[3],
                        schedule_expr=r[4],
                        action_type=r[5],
                        payload=r[6] if isinstance(r[6], dict) else json.loads(r[6]),
                        enabled=r[7],
                        next_run_at=r[8],
                        last_run_at=r[9],
                        last_status=r[10],
                        created_at=r[11],
                        updated_at=r[12],
                    )
                    for r in rows
                ]

    def claim_job(self, owner_id, job_id, *, expected_next_run, next_run, enabled, now):
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jarvis_app.scheduled_jobs
                       SET next_run_at = %s, enabled = %s, last_run_at = %s, last_status = 'running', updated_at = %s
                       WHERE id = %s AND owner_id = %s AND enabled = true AND next_run_at = %s""",
                    (next_run, enabled, now, now, job_id, owner_id, expected_next_run),
                )
                claimed = cur.rowcount == 1
            conn.commit()
            return claimed

    def record_run(self, owner_id, job_id, *, expected_next_run, next_run, enabled, last_status, last_run_at, now):
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jarvis_app.scheduled_jobs
                       SET next_run_at = %s, enabled = %s, last_status = %s, last_run_at = %s, updated_at = %s
                       WHERE id = %s AND owner_id = %s AND next_run_at IS NOT DISTINCT FROM %s""",
                    (next_run, enabled, last_status, last_run_at, now, job_id, owner_id, expected_next_run),
                )
                changed = cur.rowcount == 1
            conn.commit()
            return changed

    def fail_stale_running(self, owner_id, before, now):
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jarvis_app.scheduled_jobs SET last_status = 'failure', updated_at = %s
                       WHERE owner_id = %s AND last_status = 'running' AND (last_run_at IS NULL OR last_run_at < %s)""",
                    (now, owner_id, before),
                )
                count = cur.rowcount
            conn.commit()
            return count

    def insert_notification_if_absent(self, notification):
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jarvis_app.notifications
                       (id, owner_id, category, title, message, priority, read, delivered_at, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                    (notification.id, notification.owner_id, notification.category, notification.title,
                     notification.message, notification.priority, notification.read, notification.delivered_at,
                     notification.created_at),
                )
                inserted = cur.rowcount == 1
            conn.commit()
            return inserted

    def held_notification_exists(self, owner_id, category, title):
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT 1 FROM jarvis_app.notifications
                       WHERE owner_id = %s AND category = %s AND title = %s AND delivered_at IS NULL LIMIT 1""",
                    (owner_id, category, title),
                )
                return cur.fetchone() is not None

    def latest_delivered_at(self, owner_id, category):
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT max(delivered_at) FROM jarvis_app.notifications
                       WHERE owner_id = %s AND category = %s AND delivered_at IS NOT NULL""",
                    (owner_id, category),
                )
                return cur.fetchone()[0]

    def release_held_notifications(self, owner_id, now):
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jarvis_app.notifications SET delivered_at = %s WHERE owner_id = %s AND delivered_at IS NULL",
                    (now, owner_id),
                )
                released = cur.rowcount
            conn.commit()
            return released

    def save_notification(self, notification: Notification) -> None:
        query = """
            INSERT INTO jarvis_app.notifications (
                id, owner_id, category, title, message, priority, read, delivered_at, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                read = EXCLUDED.read,
                delivered_at = EXCLUDED.delivered_at;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    query,
                    (
                        notification.id,
                        notification.owner_id,
                        notification.category,
                        notification.title,
                        notification.message,
                        notification.priority,
                        notification.read,
                        notification.delivered_at,
                        notification.created_at,
                    ),
                )
            conn.commit()

    def get_notification(self, owner_id: UUID, notif_id: UUID) -> Notification | None:
        query = """
            SELECT id, owner_id, category, title, message, priority, read, delivered_at, created_at
            FROM jarvis_app.notifications
            WHERE id = %s AND owner_id = %s;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (notif_id, owner_id))
                row = cur.fetchone()
                if not row:
                    return None
                return Notification(
                    id=row[0],
                    owner_id=row[1],
                    category=row[2],
                    title=row[3],
                    message=row[4],
                    priority=row[5],
                    read=row[6],
                    delivered_at=row[7],
                    created_at=row[8],
                )

    def list_notifications(
        self,
        owner_id: UUID,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
        include_held: bool = True,
    ) -> tuple[list[Notification], int]:
        filter_clause = ("AND read = false " if unread_only else "") + ("" if include_held else "AND delivered_at IS NOT NULL")
        count_q = f"SELECT COUNT(*) FROM jarvis_app.notifications WHERE owner_id = %s {filter_clause};"
        query = f"""
            SELECT id, owner_id, category, title, message, priority, read, delivered_at, created_at
            FROM jarvis_app.notifications
            WHERE owner_id = %s {filter_clause}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(count_q, (owner_id,))
                total = cur.fetchone()[0]
                cur.execute(query, (owner_id, limit, offset))
                rows = cur.fetchall()
                notifications = [
                    Notification(
                        id=r[0],
                        owner_id=r[1],
                        category=r[2],
                        title=r[3],
                        message=r[4],
                        priority=r[5],
                        read=r[6],
                        delivered_at=r[7],
                        created_at=r[8],
                    )
                    for r in rows
                ]
                return notifications, total

    def mark_notification_read(self, owner_id: UUID, notif_id: UUID) -> bool:
        query = "UPDATE jarvis_app.notifications SET read = true WHERE id = %s AND owner_id = %s;"
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (notif_id, owner_id))
                updated = cur.rowcount > 0
            conn.commit()
            return updated
