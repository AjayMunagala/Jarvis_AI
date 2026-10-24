"""Assistant storage: in-memory (non-durable) and PostgreSQL (durable).

Dual-store architecture following the existing Jarvis patterns (v3.1).
PostgreSQL storage uses parameterized, owner-scoped queries under bounded
operation timeouts.
"""

from datetime import datetime, timezone
from time import monotonic
import threading
from typing import Any, Callable, Protocol
from uuid import UUID

from jarvis.assistant.models import (
    CalendarEvent, Note, Reminder, ReminderStatus, Task, TaskPriority, TaskStatus,
)
from jarvis.persistence.operation_timeout import (
    bounded_connect_timeout, execute_with_deadline, operation_deadline,
)


class AssistantStorageError(Exception):
    """Sanitized storage failure for assistant operations."""


class AssistantStore(Protocol):
    durable: bool

    # Tasks
    def create_task(self, task: Task) -> None: ...
    def get_task(self, owner_id: UUID, task_id: UUID) -> Task | None: ...
    def list_tasks(
        self,
        owner_id: UUID,
        *,
        status: TaskStatus | None = None,
        priority: TaskPriority | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Task], int]: ...
    def update_task(self, owner_id: UUID, task_id: UUID, updates: dict[str, Any]) -> Task | None: ...
    def delete_task(self, owner_id: UUID, task_id: UUID) -> bool: ...

    # Notes
    def create_note(self, note: Note) -> None: ...
    def get_note(self, owner_id: UUID, note_id: UUID) -> Note | None: ...
    def list_notes(
        self,
        owner_id: UUID,
        *,
        query: str | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Note], int]: ...
    def update_note(self, owner_id: UUID, note_id: UUID, updates: dict[str, Any]) -> Note | None: ...
    def delete_note(self, owner_id: UUID, note_id: UUID) -> bool: ...

    # Reminders
    def create_reminder(self, reminder: Reminder) -> None: ...
    def get_reminder(self, owner_id: UUID, reminder_id: UUID) -> Reminder | None: ...
    def list_reminders(
        self,
        owner_id: UUID,
        *,
        status: ReminderStatus | None = None,
        due_before: datetime | None = None,
        due_after: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Reminder], int]: ...
    def update_reminder(self, owner_id: UUID, reminder_id: UUID, updates: dict[str, Any]) -> Reminder | None: ...
    def delete_reminder(self, owner_id: UUID, reminder_id: UUID) -> bool: ...
    # v4.5: atomically move a pending reminder to "triggered"; None if it was not pending (already delivered).
    def trigger_reminder(self, owner_id: UUID, reminder_id: UUID, now: datetime) -> Reminder | None: ...

    # Calendar Events
    def create_calendar_event(self, event: CalendarEvent) -> None: ...
    def get_calendar_event(self, owner_id: UUID, event_id: UUID) -> CalendarEvent | None: ...
    def list_calendar_events(
        self,
        owner_id: UUID,
        *,
        start_range: datetime,
        end_range: datetime,
        limit: int = 100,
    ) -> list[CalendarEvent]: ...
    def update_calendar_event(self, owner_id: UUID, event_id: UUID, updates: dict[str, Any]) -> CalendarEvent | None: ...
    def delete_calendar_event(self, owner_id: UUID, event_id: UUID) -> bool: ...


# ------------------------------------------------------------- In-Memory

class MemoryAssistantStore:
    durable = False

    def __init__(self) -> None:
        self._tasks: dict[UUID, Task] = {}
        self._notes: dict[UUID, Note] = {}
        self._reminders: dict[UUID, Reminder] = {}
        self._events: dict[UUID, CalendarEvent] = {}
        self._lock = threading.Lock()

    # Tasks
    def create_task(self, task: Task) -> None:
        with self._lock:
            self._tasks[task.id] = task

    def get_task(self, owner_id: UUID, task_id: UUID) -> Task | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task if task and task.owner_id == owner_id else None

    def list_tasks(
        self,
        owner_id: UUID,
        *,
        status: TaskStatus | None = None,
        priority: TaskPriority | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Task], int]:
        with self._lock:
            matched = [
                t for t in self._tasks.values()
                if t.owner_id == owner_id
                and (status is None or t.status == status)
                and (priority is None or t.priority == priority)
                and (tag is None or tag in t.tags)
            ]
            matched.sort(key=lambda x: (x.due_date is None, x.due_date or datetime.max.replace(tzinfo=timezone.utc), x.created_at))
            total = len(matched)
            return matched[offset:offset + limit], total

    def update_task(self, owner_id: UUID, task_id: UUID, updates: dict[str, Any]) -> Task | None:
        with self._lock:
            existing = self._tasks.get(task_id)
            if not existing or existing.owner_id != owner_id:
                return None
            data = existing.model_dump()
            data.update({k: v for k, v in updates.items() if v is not None})
            data["updated_at"] = datetime.now(timezone.utc)
            updated = Task.model_validate(data)
            self._tasks[task_id] = updated
            return updated

    def delete_task(self, owner_id: UUID, task_id: UUID) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.owner_id != owner_id:
                return False
            del self._tasks[task_id]
            # Cascade null to reminders
            for rem in self._reminders.values():
                if rem.task_id == task_id:
                    data = rem.model_dump()
                    data["task_id"] = None
                    self._reminders[rem.id] = Reminder.model_validate(data)
            return True

    # Notes
    def create_note(self, note: Note) -> None:
        with self._lock:
            self._notes[note.id] = note

    def get_note(self, owner_id: UUID, note_id: UUID) -> Note | None:
        with self._lock:
            note = self._notes.get(note_id)
            return note if note and note.owner_id == owner_id else None

    def list_notes(
        self,
        owner_id: UUID,
        *,
        query: str | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Note], int]:
        with self._lock:
            q = query.lower() if query else None
            matched = [
                n for n in self._notes.values()
                if n.owner_id == owner_id
                and (tag is None or tag in n.tags)
                and (q is None or q in n.title.lower() or q in n.content.lower())
            ]
            matched.sort(key=lambda x: x.updated_at, reverse=True)
            total = len(matched)
            return matched[offset:offset + limit], total

    def update_note(self, owner_id: UUID, note_id: UUID, updates: dict[str, Any]) -> Note | None:
        with self._lock:
            existing = self._notes.get(note_id)
            if not existing or existing.owner_id != owner_id:
                return None
            data = existing.model_dump()
            data.update({k: v for k, v in updates.items() if v is not None})
            data["updated_at"] = datetime.now(timezone.utc)
            updated = Note.model_validate(data)
            self._notes[note_id] = updated
            return updated

    def delete_note(self, owner_id: UUID, note_id: UUID) -> bool:
        with self._lock:
            note = self._notes.get(note_id)
            if not note or note.owner_id != owner_id:
                return False
            del self._notes[note_id]
            return True

    # Reminders
    def create_reminder(self, reminder: Reminder) -> None:
        with self._lock:
            self._reminders[reminder.id] = reminder

    def get_reminder(self, owner_id: UUID, reminder_id: UUID) -> Reminder | None:
        with self._lock:
            reminder = self._reminders.get(reminder_id)
            return reminder if reminder and reminder.owner_id == owner_id else None

    def list_reminders(
        self,
        owner_id: UUID,
        *,
        status: ReminderStatus | None = None,
        due_before: datetime | None = None,
        due_after: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Reminder], int]:
        with self._lock:
            matched = [
                r for r in self._reminders.values()
                if r.owner_id == owner_id
                and (status is None or r.status == status)
                and (due_before is None or r.remind_at <= due_before)
                and (due_after is None or r.remind_at >= due_after)
            ]
            matched.sort(key=lambda x: x.remind_at)
            total = len(matched)
            return matched[offset:offset + limit], total

    def update_reminder(self, owner_id: UUID, reminder_id: UUID, updates: dict[str, Any]) -> Reminder | None:
        with self._lock:
            existing = self._reminders.get(reminder_id)
            if not existing or existing.owner_id != owner_id:
                return None
            data = existing.model_dump()
            data.update({k: v for k, v in updates.items() if v is not None})
            data["updated_at"] = datetime.now(timezone.utc)
            updated = Reminder.model_validate(data)
            self._reminders[reminder_id] = updated
            return updated

    def delete_reminder(self, owner_id: UUID, reminder_id: UUID) -> bool:
        with self._lock:
            reminder = self._reminders.get(reminder_id)
            if not reminder or reminder.owner_id != owner_id:
                return False
            del self._reminders[reminder_id]
            return True

    def trigger_reminder(self, owner_id: UUID, reminder_id: UUID, now: datetime) -> Reminder | None:
        with self._lock:
            reminder = self._reminders.get(reminder_id)
            if not reminder or reminder.owner_id != owner_id or reminder.status != ReminderStatus.PENDING:
                return None
            updated = reminder.model_copy(update={"status": ReminderStatus.TRIGGERED, "updated_at": now})
            self._reminders[reminder_id] = updated
            return updated

    # Calendar Events
    def create_calendar_event(self, event: CalendarEvent) -> None:
        with self._lock:
            self._events[event.id] = event

    def get_calendar_event(self, owner_id: UUID, event_id: UUID) -> CalendarEvent | None:
        with self._lock:
            ev = self._events.get(event_id)
            return ev if ev and ev.owner_id == owner_id else None

    def list_calendar_events(
        self,
        owner_id: UUID,
        *,
        start_range: datetime,
        end_range: datetime,
        limit: int = 100,
    ) -> list[CalendarEvent]:
        with self._lock:
            matched = [
                e for e in self._events.values()
                if e.owner_id == owner_id
                and e.end_time >= start_range
                and e.start_time <= end_range
            ]
            matched.sort(key=lambda x: (x.start_time, x.end_time))
            return matched[:limit]

    def update_calendar_event(self, owner_id: UUID, event_id: UUID, updates: dict[str, Any]) -> CalendarEvent | None:
        with self._lock:
            existing = self._events.get(event_id)
            if not existing or existing.owner_id != owner_id:
                return None
            data = existing.model_dump()
            data.update({k: v for k, v in updates.items() if v is not None})
            data["updated_at"] = datetime.now(timezone.utc)
            updated = CalendarEvent.model_validate(data)
            self._events[event_id] = updated
            return updated

    def delete_calendar_event(self, owner_id: UUID, event_id: UUID) -> bool:
        with self._lock:
            event = self._events.get(event_id)
            if not event or event.owner_id != owner_id:
                return False
            del self._events[event_id]
            return True


# ------------------------------------------------------------- PostgreSQL

class PostgresAssistantStore:
    durable = True

    def __init__(
        self,
        database_url: str,
        *,
        connect_timeout_seconds: int = 3,
        operation_timeout_seconds: int = 5,
        connect: Callable | None = None,
    ) -> None:
        if connect is None:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError("PostgreSQL storage requires psycopg.") from exc
            connect = psycopg.connect
        self._database_url = database_url
        self._connect = connect
        self._connect_timeout = connect_timeout_seconds
        self._operation_timeout = operation_timeout_seconds

    def _open(self, deadline: float):
        timeout = bounded_connect_timeout(self._connect_timeout, deadline, clock=monotonic)
        return self._connect(self._database_url, connect_timeout=timeout)

    # Tasks
    def create_task(self, task: Task) -> None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = """
            INSERT INTO jarvis_app.tasks
                (id, owner_id, title, description, status, priority, due_date, tags, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        try:
            with self._open(deadline) as conn:
                execute_with_deadline(
                    conn, deadline, sql,
                    (task.id, task.owner_id, task.title, task.description, task.status.value,
                     task.priority.value, task.due_date, task.tags, task.created_at, task.updated_at),
                    clock=monotonic,
                )
        except Exception as exc:
            raise AssistantStorageError("Failed to store task in PostgreSQL.") from exc

    def get_task(self, owner_id: UUID, task_id: UUID) -> Task | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = "SELECT id, owner_id, title, description, status, priority, due_date, tags, created_at, updated_at FROM jarvis_app.tasks WHERE id = %s AND owner_id = %s"
        try:
            with self._open(deadline) as conn:
                row = execute_with_deadline(conn, deadline, sql, (task_id, owner_id), clock=monotonic).fetchone()
                if not row:
                    return None
                return Task(
                    id=row[0], owner_id=row[1], title=row[2], description=row[3],
                    status=TaskStatus(row[4]), priority=TaskPriority(row[5]),
                    due_date=row[6], tags=list(row[7]) if row[7] else [],
                    created_at=row[8], updated_at=row[9]
                )
        except Exception as exc:
            raise AssistantStorageError("Failed to get task from PostgreSQL.") from exc

    def list_tasks(
        self,
        owner_id: UUID,
        *,
        status: TaskStatus | None = None,
        priority: TaskPriority | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Task], int]:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        clauses = ["owner_id = %s"]
        params: list[Any] = [owner_id]
        if status:
            clauses.append("status = %s")
            params.append(status.value)
        if priority:
            clauses.append("priority = %s")
            params.append(priority.value)
        if tag:
            clauses.append("%s = ANY(tags)")
            params.append(tag)
        where = " AND ".join(clauses)
        count_sql = f"SELECT COUNT(*) FROM jarvis_app.tasks WHERE {where}"
        data_sql = f"""
            SELECT id, owner_id, title, description, status, priority, due_date, tags, created_at, updated_at
            FROM jarvis_app.tasks
            WHERE {where}
            ORDER BY (due_date IS NULL) ASC, due_date ASC, created_at ASC
            LIMIT %s OFFSET %s
        """
        try:
            with self._open(deadline) as conn:
                total = execute_with_deadline(conn, deadline, count_sql, tuple(params), clock=monotonic).fetchone()[0]
                rows = execute_with_deadline(conn, deadline, data_sql, tuple(params + [limit, offset]), clock=monotonic).fetchall()
                items = [
                    Task(
                        id=r[0], owner_id=r[1], title=r[2], description=r[3],
                        status=TaskStatus(r[4]), priority=TaskPriority(r[5]),
                        due_date=r[6], tags=list(r[7]) if r[7] else [],
                        created_at=r[8], updated_at=r[9]
                    ) for r in rows
                ]
                return items, total
        except Exception as exc:
            raise AssistantStorageError("Failed to list tasks from PostgreSQL.") from exc

    def update_task(self, owner_id: UUID, task_id: UUID, updates: dict[str, Any]) -> Task | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        set_clauses = ["updated_at = %s"]
        now = datetime.now(timezone.utc)
        params: list[Any] = [now]
        for key in ("title", "description", "status", "priority", "due_date", "tags"):
            if key in updates and updates[key] is not None:
                val = updates[key]
                if isinstance(val, (TaskStatus, TaskPriority)):
                    val = val.value
                set_clauses.append(f"{key} = %s")
                params.append(val)
        params.extend([task_id, owner_id])
        sql = f"""
            UPDATE jarvis_app.tasks
            SET {', '.join(set_clauses)}
            WHERE id = %s AND owner_id = %s
            RETURNING id, owner_id, title, description, status, priority, due_date, tags, created_at, updated_at
        """
        try:
            with self._open(deadline) as conn:
                row = execute_with_deadline(conn, deadline, sql, tuple(params), clock=monotonic).fetchone()
                if not row:
                    return None
                return Task(
                    id=row[0], owner_id=row[1], title=row[2], description=row[3],
                    status=TaskStatus(row[4]), priority=TaskPriority(row[5]),
                    due_date=row[6], tags=list(row[7]) if row[7] else [],
                    created_at=row[8], updated_at=row[9]
                )
        except Exception as exc:
            raise AssistantStorageError("Failed to update task in PostgreSQL.") from exc

    def delete_task(self, owner_id: UUID, task_id: UUID) -> bool:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = "DELETE FROM jarvis_app.tasks WHERE id = %s AND owner_id = %s"
        try:
            with self._open(deadline) as conn:
                cur = execute_with_deadline(conn, deadline, sql, (task_id, owner_id), clock=monotonic)
                return bool(cur.rowcount and cur.rowcount > 0)
        except Exception as exc:
            raise AssistantStorageError("Failed to delete task in PostgreSQL.") from exc

    # Notes
    def create_note(self, note: Note) -> None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = "INSERT INTO jarvis_app.notes (id, owner_id, title, content, tags, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
        try:
            with self._open(deadline) as conn:
                execute_with_deadline(conn, deadline, sql, (note.id, note.owner_id, note.title, note.content, note.tags, note.created_at, note.updated_at), clock=monotonic)
        except Exception as exc:
            raise AssistantStorageError("Failed to store note in PostgreSQL.") from exc

    def get_note(self, owner_id: UUID, note_id: UUID) -> Note | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = "SELECT id, owner_id, title, content, tags, created_at, updated_at FROM jarvis_app.notes WHERE id = %s AND owner_id = %s"
        try:
            with self._open(deadline) as conn:
                row = execute_with_deadline(conn, deadline, sql, (note_id, owner_id), clock=monotonic).fetchone()
                if not row:
                    return None
                return Note(id=row[0], owner_id=row[1], title=row[2], content=row[3], tags=list(row[4]) if row[4] else [], created_at=row[5], updated_at=row[6])
        except Exception as exc:
            raise AssistantStorageError("Failed to get note from PostgreSQL.") from exc

    def list_notes(
        self,
        owner_id: UUID,
        *,
        query: str | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Note], int]:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        clauses = ["owner_id = %s"]
        params: list[Any] = [owner_id]
        if tag:
            clauses.append("%s = ANY(tags)")
            params.append(tag)
        if query:
            clauses.append("(title ILIKE %s OR content ILIKE %s)")
            pattern = f"%{query}%"
            params.extend([pattern, pattern])
        where = " AND ".join(clauses)
        count_sql = f"SELECT COUNT(*) FROM jarvis_app.notes WHERE {where}"
        data_sql = f"SELECT id, owner_id, title, content, tags, created_at, updated_at FROM jarvis_app.notes WHERE {where} ORDER BY updated_at DESC LIMIT %s OFFSET %s"
        try:
            with self._open(deadline) as conn:
                total = execute_with_deadline(conn, deadline, count_sql, tuple(params), clock=monotonic).fetchone()[0]
                rows = execute_with_deadline(conn, deadline, data_sql, tuple(params + [limit, offset]), clock=monotonic).fetchall()
                items = [Note(id=r[0], owner_id=r[1], title=r[2], content=r[3], tags=list(r[4]) if r[4] else [], created_at=r[5], updated_at=r[6]) for r in rows]
                return items, total
        except Exception as exc:
            raise AssistantStorageError("Failed to list notes from PostgreSQL.") from exc

    def update_note(self, owner_id: UUID, note_id: UUID, updates: dict[str, Any]) -> Note | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        set_clauses = ["updated_at = %s"]
        now = datetime.now(timezone.utc)
        params: list[Any] = [now]
        for key in ("title", "content", "tags"):
            if key in updates and updates[key] is not None:
                set_clauses.append(f"{key} = %s")
                params.append(updates[key])
        params.extend([note_id, owner_id])
        sql = f"UPDATE jarvis_app.notes SET {', '.join(set_clauses)} WHERE id = %s AND owner_id = %s RETURNING id, owner_id, title, content, tags, created_at, updated_at"
        try:
            with self._open(deadline) as conn:
                row = execute_with_deadline(conn, deadline, sql, tuple(params), clock=monotonic).fetchone()
                if not row:
                    return None
                return Note(id=row[0], owner_id=row[1], title=row[2], content=row[3], tags=list(row[4]) if row[4] else [], created_at=row[5], updated_at=row[6])
        except Exception as exc:
            raise AssistantStorageError("Failed to update note in PostgreSQL.") from exc

    def delete_note(self, owner_id: UUID, note_id: UUID) -> bool:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = "DELETE FROM jarvis_app.notes WHERE id = %s AND owner_id = %s"
        try:
            with self._open(deadline) as conn:
                cur = execute_with_deadline(conn, deadline, sql, (note_id, owner_id), clock=monotonic)
                return bool(cur.rowcount and cur.rowcount > 0)
        except Exception as exc:
            raise AssistantStorageError("Failed to delete note in PostgreSQL.") from exc

    # Reminders
    def create_reminder(self, reminder: Reminder) -> None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = "INSERT INTO jarvis_app.reminders (id, owner_id, task_id, title, remind_at, status, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        try:
            with self._open(deadline) as conn:
                execute_with_deadline(conn, deadline, sql, (reminder.id, reminder.owner_id, reminder.task_id, reminder.title, reminder.remind_at, reminder.status.value, reminder.created_at, reminder.updated_at), clock=monotonic)
        except Exception as exc:
            raise AssistantStorageError("Failed to store reminder in PostgreSQL.") from exc

    def get_reminder(self, owner_id: UUID, reminder_id: UUID) -> Reminder | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = "SELECT id, owner_id, task_id, title, remind_at, status, created_at, updated_at FROM jarvis_app.reminders WHERE id = %s AND owner_id = %s"
        try:
            with self._open(deadline) as conn:
                row = execute_with_deadline(conn, deadline, sql, (reminder_id, owner_id), clock=monotonic).fetchone()
                if not row:
                    return None
                return Reminder(id=row[0], owner_id=row[1], task_id=row[2], title=row[3], remind_at=row[4], status=ReminderStatus(row[5]), created_at=row[6], updated_at=row[7])
        except Exception as exc:
            raise AssistantStorageError("Failed to get reminder from PostgreSQL.") from exc

    def list_reminders(
        self,
        owner_id: UUID,
        *,
        status: ReminderStatus | None = None,
        due_before: datetime | None = None,
        due_after: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Reminder], int]:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        clauses = ["owner_id = %s"]
        params: list[Any] = [owner_id]
        if status:
            clauses.append("status = %s")
            params.append(status.value)
        if due_before:
            clauses.append("remind_at <= %s")
            params.append(due_before)
        if due_after:  # v6.3: lets the briefing skip stale past-due reminders in SQL
            clauses.append("remind_at >= %s")
            params.append(due_after)
        where = " AND ".join(clauses)
        count_sql = f"SELECT COUNT(*) FROM jarvis_app.reminders WHERE {where}"
        data_sql = f"SELECT id, owner_id, task_id, title, remind_at, status, created_at, updated_at FROM jarvis_app.reminders WHERE {where} ORDER BY remind_at ASC LIMIT %s OFFSET %s"
        try:
            with self._open(deadline) as conn:
                total = execute_with_deadline(conn, deadline, count_sql, tuple(params), clock=monotonic).fetchone()[0]
                rows = execute_with_deadline(conn, deadline, data_sql, tuple(params + [limit, offset]), clock=monotonic).fetchall()
                items = [Reminder(id=r[0], owner_id=r[1], task_id=r[2], title=r[3], remind_at=r[4], status=ReminderStatus(r[5]), created_at=r[6], updated_at=r[7]) for r in rows]
                return items, total
        except Exception as exc:
            raise AssistantStorageError("Failed to list reminders from PostgreSQL.") from exc

    def update_reminder(self, owner_id: UUID, reminder_id: UUID, updates: dict[str, Any]) -> Reminder | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        set_clauses = ["updated_at = %s"]
        now = datetime.now(timezone.utc)
        params: list[Any] = [now]
        for key in ("title", "remind_at", "status"):
            if key in updates and updates[key] is not None:
                val = updates[key]
                if isinstance(val, ReminderStatus):
                    val = val.value
                set_clauses.append(f"{key} = %s")
                params.append(val)
        params.extend([reminder_id, owner_id])
        sql = f"UPDATE jarvis_app.reminders SET {', '.join(set_clauses)} WHERE id = %s AND owner_id = %s RETURNING id, owner_id, task_id, title, remind_at, status, created_at, updated_at"
        try:
            with self._open(deadline) as conn:
                row = execute_with_deadline(conn, deadline, sql, tuple(params), clock=monotonic).fetchone()
                if not row:
                    return None
                return Reminder(id=row[0], owner_id=row[1], task_id=row[2], title=row[3], remind_at=row[4], status=ReminderStatus(row[5]), created_at=row[6], updated_at=row[7])
        except Exception as exc:
            raise AssistantStorageError("Failed to update reminder in PostgreSQL.") from exc

    def delete_reminder(self, owner_id: UUID, reminder_id: UUID) -> bool:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = "DELETE FROM jarvis_app.reminders WHERE id = %s AND owner_id = %s"
        try:
            with self._open(deadline) as conn:
                cur = execute_with_deadline(conn, deadline, sql, (reminder_id, owner_id), clock=monotonic)
                return bool(cur.rowcount and cur.rowcount > 0)
        except Exception as exc:
            raise AssistantStorageError("Failed to delete reminder in PostgreSQL.") from exc

    def trigger_reminder(self, owner_id: UUID, reminder_id: UUID, now: datetime) -> Reminder | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = ("UPDATE jarvis_app.reminders SET status = 'triggered', updated_at = %s "
               "WHERE id = %s AND owner_id = %s AND status = 'pending' "
               "RETURNING id, owner_id, task_id, title, remind_at, status, created_at, updated_at")
        try:
            with self._open(deadline) as conn:
                row = execute_with_deadline(conn, deadline, sql, (now, reminder_id, owner_id), clock=monotonic).fetchone()
                if not row:
                    return None
                return Reminder(id=row[0], owner_id=row[1], task_id=row[2], title=row[3], remind_at=row[4],
                                status=ReminderStatus(row[5]), created_at=row[6], updated_at=row[7])
        except Exception as exc:
            raise AssistantStorageError("Failed to trigger reminder in PostgreSQL.") from exc

    # Calendar Events
    def create_calendar_event(self, event: CalendarEvent) -> None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = "INSERT INTO jarvis_app.calendar_events (id, owner_id, title, description, start_time, end_time, location, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        try:
            with self._open(deadline) as conn:
                execute_with_deadline(conn, deadline, sql, (event.id, event.owner_id, event.title, event.description, event.start_time, event.end_time, event.location, event.created_at, event.updated_at), clock=monotonic)
        except Exception as exc:
            raise AssistantStorageError("Failed to store calendar event in PostgreSQL.") from exc

    def get_calendar_event(self, owner_id: UUID, event_id: UUID) -> CalendarEvent | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = "SELECT id, owner_id, title, description, start_time, end_time, location, created_at, updated_at FROM jarvis_app.calendar_events WHERE id = %s AND owner_id = %s"
        try:
            with self._open(deadline) as conn:
                row = execute_with_deadline(conn, deadline, sql, (event_id, owner_id), clock=monotonic).fetchone()
                if not row:
                    return None
                return CalendarEvent(id=row[0], owner_id=row[1], title=row[2], description=row[3], start_time=row[4], end_time=row[5], location=row[6], created_at=row[7], updated_at=row[8])
        except Exception as exc:
            raise AssistantStorageError("Failed to get calendar event from PostgreSQL.") from exc

    def list_calendar_events(
        self,
        owner_id: UUID,
        *,
        start_range: datetime,
        end_range: datetime,
        limit: int = 100,
    ) -> list[CalendarEvent]:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = """
            SELECT id, owner_id, title, description, start_time, end_time, location, created_at, updated_at
            FROM jarvis_app.calendar_events
            WHERE owner_id = %s AND end_time >= %s AND start_time <= %s
            ORDER BY start_time ASC, end_time ASC
            LIMIT %s
        """
        try:
            with self._open(deadline) as conn:
                rows = execute_with_deadline(conn, deadline, sql, (owner_id, start_range, end_range, limit), clock=monotonic).fetchall()
                return [CalendarEvent(id=r[0], owner_id=r[1], title=r[2], description=r[3], start_time=r[4], end_time=r[5], location=r[6], created_at=r[7], updated_at=r[8]) for r in rows]
        except Exception as exc:
            raise AssistantStorageError("Failed to list calendar events from PostgreSQL.") from exc

    def update_calendar_event(self, owner_id: UUID, event_id: UUID, updates: dict[str, Any]) -> CalendarEvent | None:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        set_clauses = ["updated_at = %s"]
        now = datetime.now(timezone.utc)
        params: list[Any] = [now]
        for key in ("title", "description", "start_time", "end_time", "location"):
            if key in updates and updates[key] is not None:
                set_clauses.append(f"{key} = %s")
                params.append(updates[key])
        params.extend([event_id, owner_id])
        sql = f"""
            UPDATE jarvis_app.calendar_events
            SET {', '.join(set_clauses)}
            WHERE id = %s AND owner_id = %s
            RETURNING id, owner_id, title, description, start_time, end_time, location, created_at, updated_at
        """
        try:
            with self._open(deadline) as conn:
                row = execute_with_deadline(conn, deadline, sql, tuple(params), clock=monotonic).fetchone()
                if not row:
                    return None
                return CalendarEvent(id=row[0], owner_id=row[1], title=row[2], description=row[3], start_time=row[4], end_time=row[5], location=row[6], created_at=row[7], updated_at=row[8])
        except Exception as exc:
            raise AssistantStorageError("Failed to update calendar event in PostgreSQL.") from exc

    def delete_calendar_event(self, owner_id: UUID, event_id: UUID) -> bool:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        sql = "DELETE FROM jarvis_app.calendar_events WHERE id = %s AND owner_id = %s"
        try:
            with self._open(deadline) as conn:
                cur = execute_with_deadline(conn, deadline, sql, (event_id, owner_id), clock=monotonic)
                return bool(cur.rowcount and cur.rowcount > 0)
        except Exception as exc:
            raise AssistantStorageError("Failed to delete calendar event in PostgreSQL.") from exc
