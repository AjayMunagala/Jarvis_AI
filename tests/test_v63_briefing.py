"""v6.3 daily briefing and calendar conflict detection."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.assistant.briefing import build_briefing, find_conflicts
from jarvis.assistant.models import (CalendarEventCreateRequest, ReminderCreateRequest, TaskCreateRequest,
                                     TaskUpdateRequest)
from jarvis.assistant.service import AssistantService
from jarvis.assistant.stores import MemoryAssistantStore
from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.scheduler.models import JobCreateRequest
from jarvis.scheduler.service import SchedulerService
from jarvis.scheduler.stores import MemorySchedulerStore

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TOKEN = "v63-briefing-token-with-at-least-32-chars"
NOW = datetime(2026, 10, 24, 6, 30, tzinfo=timezone.utc)  # 12:00 in UTC+05:30


def event(service, title, start_hour, end_hour, day_offset=0):
    base = datetime(2026, 10, 24, tzinfo=timezone.utc) + timedelta(days=day_offset)
    return service.create_calendar_event(OWNER, CalendarEventCreateRequest(
        title=title, start_time=base + timedelta(hours=start_hour), end_time=base + timedelta(hours=end_hour)))


def seeded():
    service = AssistantService(MemoryAssistantStore())
    service.create_task(OWNER, TaskCreateRequest(title="File taxes", due_date=NOW - timedelta(days=2)))
    service.create_task(OWNER, TaskCreateRequest(title="Send report", due_date=NOW + timedelta(hours=3)))
    service.create_task(OWNER, TaskCreateRequest(title="Plan trip", due_date=NOW + timedelta(days=3)))
    done = service.create_task(OWNER, TaskCreateRequest(title="Already done", due_date=NOW - timedelta(days=1)))
    service.update_task(OWNER, done.id, TaskUpdateRequest(status="completed"))
    service.create_reminder(OWNER, ReminderCreateRequest(title="Call mom", remind_at=NOW + timedelta(hours=5)))
    service.create_reminder(OWNER, ReminderCreateRequest(title="Next week", remind_at=NOW + timedelta(days=7)))
    event(service, "Standup", 4, 5)
    event(service, "Design review", 8, 10)
    event(service, "Dentist", 9, 11)  # overlaps the design review by one hour
    event(service, "Lunch", 11, 12)  # touches the dentist exactly: not a conflict
    return service


def test_the_briefing_sorts_tasks_events_and_reminders_into_the_local_day():
    briefing = build_briefing(seeded(), OWNER, now=NOW, utc_offset_minutes=330)
    assert briefing.day.isoformat() == "2026-10-24"
    assert [task.title for task in briefing.overdue_tasks] == ["File taxes"]  # completed tasks are excluded
    assert [task.title for task in briefing.due_today] == ["Send report"]
    assert [item.title for item in briefing.reminders_next_24h] == ["Call mom"]
    assert "Standup" in [item.title for item in briefing.events_today]
    assert len(briefing.conflicts_next_7_days) == 1
    conflict = briefing.conflicts_next_7_days[0]
    assert {conflict.first.title, conflict.second.title} == {"Design review", "Dentist"} and conflict.overlap_minutes == 60


def test_the_summary_uses_only_stored_records():
    summary = build_briefing(seeded(), OWNER, now=NOW, utc_offset_minutes=330).summary
    assert summary.startswith("Briefing for 2026-10-24:")
    assert "1 overdue task(s): File taxes" in summary and "Send report" in summary
    assert "Call mom" in summary and "'Design review' overlaps 'Dentist'" in summary
    empty = build_briefing(AssistantService(MemoryAssistantStore()), OWNER, now=NOW).summary
    assert empty.endswith("Nothing overdue, due, scheduled or conflicting today.")


def test_conflicts_are_listed_once_per_pair_and_touching_events_do_not_count():
    service = AssistantService(MemoryAssistantStore())
    events = [event(service, "A", 9, 12), event(service, "B", 10, 11), event(service, "C", 11, 13),
              event(service, "D", 13, 14)]
    pairs = {(item.first.title, item.second.title) for item in find_conflicts(events)}
    assert pairs == {("A", "B"), ("A", "C")}  # B-C and C-D only touch


def test_briefing_and_conflict_endpoints():
    app = create_app(settings=Settings(_env_file=None, environment="test", log_level="ERROR",
                                       local_auth_token=SecretStr(TOKEN), owner_id=OWNER))
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        tomorrow = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
        for title, start, end in (("Board call", 0, 2), ("Gym", 1, 3)):
            client.post("/assistant/calendar/events", json={
                "title": title, "start_time": (tomorrow + timedelta(hours=start)).isoformat(),
                "end_time": (tomorrow + timedelta(hours=end)).isoformat()})
        briefing = client.get("/assistant/briefing", params={"utc_offset_minutes": 0})
        conflicts = client.get("/assistant/calendar/conflicts", params={"days": 3})
        unauthenticated = client.get("/assistant/briefing", headers={"Authorization": "Bearer nope-nope-nope-nope-nope-nope-nope"})
        bad_offset = client.get("/assistant/briefing", params={"utc_offset_minutes": 900})
        negative = client.get("/assistant/briefing", params={"utc_offset_minutes": -300})
    assert briefing.status_code == 200 and len(briefing.json()["conflicts_next_7_days"]) == 1
    assert conflicts.status_code == 200 and conflicts.json()["conflicts"][0]["overlap_minutes"] == 60
    assert conflicts.json()["truncated"] is False
    assert unauthenticated.status_code == 401 and bad_offset.status_code == 422
    assert negative.status_code == 200 and negative.json()["utc_offset_minutes"] == -300


def test_a_scheduled_daily_briefing_is_delivered_as_a_notification():
    assistant = seeded()
    scheduler = SchedulerService(MemorySchedulerStore())
    scheduler.briefing_builder = lambda owner: build_briefing(assistant, owner, now=NOW, utc_offset_minutes=330)
    job = scheduler.create_job(OWNER, JobCreateRequest(name="Morning", schedule_type="cron", schedule_expr="0 7 * * *",
                                                       action_type="daily_briefing"))
    result = scheduler.execute_job(OWNER, job.id)
    notes = scheduler.store.list_notifications(OWNER, unread_only=False, limit=10, offset=0)[0]
    assert result.status == "success" and result.output["conflicts"] == 1
    assert notes[0].category == "briefing" and notes[0].title == "Daily briefing for 2026-10-24"
    assert "File taxes" in notes[0].message
    unconnected = SchedulerService(MemorySchedulerStore())
    job = unconnected.create_job(OWNER, JobCreateRequest(name="M", schedule_type="cron", schedule_expr="0 7 * * *",
                                                         action_type="daily_briefing"))
    assert unconnected.execute_job(OWNER, job.id).status == "failure"


def test_stale_past_reminders_do_not_hide_upcoming_ones():
    service = AssistantService(MemoryAssistantStore())
    for index in range(205):
        service.create_reminder(OWNER, ReminderCreateRequest(title=f"Old {index}", remind_at=NOW - timedelta(days=1, minutes=index)))
    service.create_reminder(OWNER, ReminderCreateRequest(title="Soon", remind_at=NOW + timedelta(hours=2)))
    briefing = build_briefing(service, OWNER, now=NOW)
    assert [item.title for item in briefing.reminders_next_24h] == ["Soon"] and briefing.truncated is False


def test_events_touching_the_day_boundary_belong_to_the_neighbouring_day():
    service = AssistantService(MemoryAssistantStore())
    event(service, "Late yesterday", -1, 0)  # ends exactly at midnight
    event(service, "Early tomorrow", 24, 25)  # starts exactly at the next midnight
    event(service, "Across midnight", 23, 25)
    event(service, "Instant", 12, 12)
    titles = [item.title for item in build_briefing(service, OWNER, now=NOW, utc_offset_minutes=0).events_today]
    assert titles == ["Instant", "Across midnight"]


def test_the_local_day_for_a_negative_offset():
    service = AssistantService(MemoryAssistantStore())
    service.create_task(OWNER, TaskCreateRequest(title="Evening", due_date=datetime(2026, 10, 24, 3, tzinfo=timezone.utc)))
    # 06:30 UTC is 01:30 on the 24th at UTC-05:00; 03:00 UTC was 22:00 on the 23rd, so the task is overdue.
    briefing = build_briefing(service, OWNER, now=NOW, utc_offset_minutes=-300)
    assert briefing.day.isoformat() == "2026-10-24" and [task.title for task in briefing.overdue_tasks] == ["Evening"]


def test_conflict_detection_reports_truncation_when_the_event_limit_is_hit():
    from jarvis.assistant import briefing as module
    from jarvis.assistant.briefing import conflicts_between

    service = AssistantService(MemoryAssistantStore())
    for hour in range(6):
        event(service, f"E{hour}", hour, hour + 2)
    original = module.CONFLICT_EVENT_LIMIT
    module.CONFLICT_EVENT_LIMIT = 4
    try:
        report = conflicts_between(service, OWNER, NOW - timedelta(days=1), NOW + timedelta(days=1))
    finally:
        module.CONFLICT_EVENT_LIMIT = original
    assert report.truncated is True
    full = conflicts_between(service, OWNER, NOW - timedelta(days=1), NOW + timedelta(days=1))
    assert full.truncated is False and len(full.conflicts) == 5


def test_a_second_briefing_is_not_dropped_by_the_throttle():
    assistant = seeded()
    scheduler = SchedulerService(MemorySchedulerStore())
    scheduler.briefing_builder = lambda owner: build_briefing(assistant, owner, now=NOW, utc_offset_minutes=330)
    job = scheduler.create_job(OWNER, JobCreateRequest(name="Morning", schedule_type="cron", schedule_expr="30 1 * * *",
                                                       action_type="daily_briefing"))
    first, second = scheduler.execute_job(OWNER, job.id), scheduler.execute_job(OWNER, job.id)
    assert first.output["notified"] is True and second.output["notified"] is True
