"""Daily briefing and calendar conflict detection (v6.3).

The briefing is assembled directly from the owner's stored tasks, reminders
and calendar events. No model writes it, so it cannot invent anything; every
line comes from a record. "Today" is the owner's local day (UTC plus the
configured offset, the same setting the scheduler's quiet hours use).

Conflicts are calendar events whose times overlap (back-to-back events that
touch at one instant do not count). Each overlapping pair is listed once.
"""

from datetime import date, datetime, time as clock_time, timedelta, timezone
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from jarvis.assistant.models import CalendarEvent, ReminderStatus, Task, TaskStatus

MAX_ITEMS = 50


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BriefingTask(Frozen):
    id: UUID
    title: str
    priority: str
    due_date: datetime | None


class BriefingReminder(Frozen):
    id: UUID
    title: str
    remind_at: datetime


class BriefingEvent(Frozen):
    id: UUID
    title: str
    start_time: datetime
    end_time: datetime
    location: str | None


class Conflict(Frozen):
    first: BriefingEvent
    second: BriefingEvent
    overlap_minutes: int


class DailyBriefing(Frozen):
    day: date
    utc_offset_minutes: int
    overdue_tasks: tuple[BriefingTask, ...]
    due_today: tuple[BriefingTask, ...]
    events_today: tuple[BriefingEvent, ...]
    reminders_next_24h: tuple[BriefingReminder, ...]
    conflicts_next_7_days: tuple[Conflict, ...]
    summary: str
    truncated: bool


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _event(event: CalendarEvent) -> BriefingEvent:
    return BriefingEvent(id=event.id, title=event.title, start_time=_aware(event.start_time),
                         end_time=_aware(event.end_time), location=event.location)


def find_conflicts(events: list[CalendarEvent]) -> tuple[Conflict, ...]:
    ordered = sorted((_event(event) for event in events), key=lambda item: (item.start_time, item.end_time, str(item.id)))
    conflicts = []
    for index, first in enumerate(ordered):
        for second in ordered[index + 1:]:
            if second.start_time >= first.end_time:
                break  # sorted by start: nothing later can overlap ``first``
            overlap = min(first.end_time, second.end_time) - second.start_time
            if overlap > timedelta(0):
                conflicts.append(Conflict(first=first, second=second, overlap_minutes=max(1, int(overlap.total_seconds() // 60))))
    return tuple(conflicts)


def conflicts_between(service, owner_id: UUID, start: datetime, end: datetime) -> tuple[Conflict, ...]:
    events = service.list_calendar_events(owner_id, start_range=start, end_range=end, limit=500).items
    return find_conflicts(list(events))


def _local_day_bounds(now: datetime, offset_minutes: int) -> tuple[date, datetime, datetime]:
    offset = timedelta(minutes=offset_minutes)
    local_day = (now + offset).date()
    start = datetime.combine(local_day, clock_time.min, tzinfo=timezone.utc) - offset
    return local_day, start, start + timedelta(days=1)


def build_briefing(service, owner_id: UUID, *, now: datetime | None = None, utc_offset_minutes: int = 0) -> DailyBriefing:
    now = _aware(now or datetime.now(timezone.utc))
    day, day_start, day_end = _local_day_bounds(now, utc_offset_minutes)
    open_tasks: list[Task] = []
    for status in (TaskStatus.IN_PROGRESS, TaskStatus.PENDING):
        open_tasks.extend(service.list_tasks(owner_id, status=status, limit=200).items)
    dated = sorted((task for task in open_tasks if task.due_date is not None), key=lambda task: _aware(task.due_date))
    overdue = [task for task in dated if _aware(task.due_date) < day_start]
    due_today = [task for task in dated if day_start <= _aware(task.due_date) < day_end]
    events_today = sorted(service.list_calendar_events(owner_id, start_range=day_start, end_range=day_end,
                                                       limit=200).items, key=lambda event: _aware(event.start_time))
    reminders = service.list_reminders(owner_id, status=ReminderStatus.PENDING, due_before=now + timedelta(hours=24),
                                       limit=200).items
    upcoming = sorted((reminder for reminder in reminders if _aware(reminder.remind_at) >= now),
                      key=lambda reminder: _aware(reminder.remind_at))
    conflicts = conflicts_between(service, owner_id, day_start, day_start + timedelta(days=7))
    truncated = any(len(items) > MAX_ITEMS for items in (overdue, due_today, events_today, upcoming, conflicts))

    def task(item: Task) -> BriefingTask:
        return BriefingTask(id=item.id, title=item.title, priority=item.priority.value,
                            due_date=_aware(item.due_date) if item.due_date else None)

    briefing = DailyBriefing(
        day=day, utc_offset_minutes=utc_offset_minutes,
        overdue_tasks=tuple(task(item) for item in overdue[:MAX_ITEMS]),
        due_today=tuple(task(item) for item in due_today[:MAX_ITEMS]),
        events_today=tuple(_event(item) for item in events_today[:MAX_ITEMS]),
        reminders_next_24h=tuple(BriefingReminder(id=item.id, title=item.title, remind_at=_aware(item.remind_at))
                                 for item in upcoming[:MAX_ITEMS]),
        conflicts_next_7_days=conflicts[:MAX_ITEMS], summary="", truncated=truncated)
    return briefing.model_copy(update={"summary": summarize(briefing)})


def _clock(value: datetime, offset_minutes: int) -> str:
    return (value + timedelta(minutes=offset_minutes)).strftime("%H:%M")


def summarize(briefing: DailyBriefing) -> str:
    offset = briefing.utc_offset_minutes
    lines = [f"Briefing for {briefing.day.isoformat()}:"]
    if briefing.overdue_tasks:
        lines.append(f"{len(briefing.overdue_tasks)} overdue task(s): "
                     + "; ".join(item.title for item in briefing.overdue_tasks[:5]) + ".")
    if briefing.due_today:
        lines.append(f"{len(briefing.due_today)} task(s) due today: "
                     + "; ".join(item.title for item in briefing.due_today[:5]) + ".")
    if briefing.events_today:
        lines.append("Today's events: " + "; ".join(
            f"{_clock(item.start_time, offset)} {item.title}" for item in briefing.events_today[:8]) + ".")
    if briefing.reminders_next_24h:
        lines.append("Reminders in the next 24 hours: " + "; ".join(
            f"{_clock(item.remind_at, offset)} {item.title}" for item in briefing.reminders_next_24h[:5]) + ".")
    if briefing.conflicts_next_7_days:
        lines.append(f"{len(briefing.conflicts_next_7_days)} calendar conflict(s) in the next 7 days, first: "
                     f"'{briefing.conflicts_next_7_days[0].first.title}' overlaps "
                     f"'{briefing.conflicts_next_7_days[0].second.title}'.")
    if len(lines) == 1:
        lines.append("Nothing overdue, due, scheduled or conflicting today.")
    if briefing.truncated:
        lines.append("Some lists were shortened; open the assistant for everything.")
    return " ".join(lines)
