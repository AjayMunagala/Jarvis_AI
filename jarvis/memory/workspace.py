"""Read-only workspace context for chat (v4.7).

When ``JARVIS_CHAT_WORKSPACE_CONTEXT_ENABLED=true``, chat adds two kinds of
reference records to the model request, next to conversation history,
memories and documents:

- knowledge records (``K`` refs): the best-matching chunks from the knowledge
  store (v3.3/v4.6), including research documents indexed there;
- workspace records (``W`` refs): one-line summaries of the owner's open tasks,
  reminders due in the next 48 hours and calendar events in the next 48 hours
  (v3.1).

Everything here only reads. Chat never creates, changes or deletes a task,
reminder, event or document; those stay explicit actions in their own views
and APIs. Each source is used only when the owner's policy grants its read
capability, and a source that fails is skipped and reported instead of
failing the chat request. The records are rendered with the other context as
untrusted JSON data.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Callable
from uuid import UUID

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=48)


@dataclass(frozen=True)
class WorkspaceRecord:
    id: UUID
    content: str
    source_kind: str  # "knowledge" or "workspace"
    label: str
    updated_at: datetime | None = None


@dataclass(frozen=True)
class WorkspaceGathering:
    records: tuple[WorkspaceRecord, ...]
    status: str  # "included", "empty" or "unavailable" (every permitted source failed)
    skipped: tuple[str, ...] = ()  # sources skipped: not permitted or failed


def _when(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if value else "no due date"


def gather_workspace_records(state: Any, owner_id: UUID, message: str, *, allows: Callable[[str], bool],
                             now: datetime | None = None, knowledge_limit: int = 3,
                             item_limit: int = 5) -> WorkspaceGathering:
    now = now or datetime.now(timezone.utc)
    records: list[WorkspaceRecord] = []
    skipped: list[str] = []
    attempted = failed = 0

    knowledge = getattr(state, "knowledge_service", None)
    query = " ".join(message.split())[:200]
    if knowledge is not None and query and allows("knowledge.read"):
        attempted += 1
        try:
            from jarvis.knowledge.models import KnowledgeSearchRequest

            for hit in knowledge.search(owner_id, KnowledgeSearchRequest(query=query, top_k=knowledge_limit)).results:
                # v4.6 marks chunks of untrusted documents (for example indexed research files); the label says so.
                label = ("untrusted document: " if hit.chunk.untrusted else "") + hit.document_title
                records.append(WorkspaceRecord(id=hit.chunk.id, content=hit.chunk.content, source_kind="knowledge",
                                               label=label[:200], updated_at=hit.chunk.created_at))
        except Exception as exc:  # noqa: BLE001 - context is helpful, not required
            failed += 1
            skipped.append("knowledge (unavailable)")
            logger.warning("Knowledge context unavailable for chat: %s", type(exc).__name__)
    elif knowledge is not None and query:
        skipped.append("knowledge (not permitted)")

    assistant = getattr(state, "assistant_service", None)
    if assistant is not None and allows("assistant.read"):
        attempted += 1
        try:
            from jarvis.assistant.models import ReminderStatus, TaskStatus

            tasks = [task for status in (TaskStatus.IN_PROGRESS, TaskStatus.PENDING)
                     for task in assistant.list_tasks(owner_id, status=status, limit=50).items]
            tasks.sort(key=lambda task: (task.due_date is None, task.due_date or now, task.title))
            for task in tasks[:item_limit]:
                records.append(WorkspaceRecord(
                    id=task.id, source_kind="workspace", label="task", updated_at=task.updated_at,
                    content=f"Task: {task.title} (status {task.status.value}, priority {task.priority.value}, "
                            f"due {_when(task.due_date)})"))
            for reminder in assistant.list_reminders(owner_id, status=ReminderStatus.PENDING, due_before=now + WINDOW,
                                                     limit=item_limit).items:
                records.append(WorkspaceRecord(
                    id=reminder.id, source_kind="workspace", label="reminder", updated_at=reminder.updated_at,
                    content=f"Reminder: {reminder.title} at {_when(reminder.remind_at)}"))
            for event in assistant.list_calendar_events(owner_id, start_range=now, end_range=now + WINDOW,
                                                        limit=item_limit).items:
                place = f" at {event.location}" if getattr(event, "location", None) else ""
                records.append(WorkspaceRecord(
                    id=event.id, source_kind="workspace", label="calendar event", updated_at=event.updated_at,
                    content=f"Event: {event.title} from {_when(event.start_time)} to {_when(event.end_time)}{place}"))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            skipped.append("assistant (unavailable)")
            logger.warning("Assistant context unavailable for chat: %s", type(exc).__name__)
    elif assistant is not None:
        skipped.append("assistant (not permitted)")

    if records:
        status = "included"
    elif attempted and failed == attempted:
        status = "unavailable"
    else:
        status = "empty"
    return WorkspaceGathering(records=tuple(records), status=status, skipped=tuple(skipped))
