"""v6.3 live PostgreSQL validation: the briefing reads upcoming reminders past a backlog of stale ones."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4


def test_the_briefing_skips_stale_reminders_in_sql(isolated_postgres):
    from jarvis.assistant.briefing import build_briefing
    from jarvis.assistant.models import CalendarEventCreateRequest, ReminderCreateRequest
    from jarvis.assistant.service import AssistantService
    from jarvis.assistant.stores import PostgresAssistantStore

    owner = uuid4()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    service = AssistantService(PostgresAssistantStore(isolated_postgres.database_url))
    for index in range(205):
        service.create_reminder(owner, ReminderCreateRequest(title=f"Old {index}", remind_at=now - timedelta(days=2, minutes=index)))
    service.create_reminder(owner, ReminderCreateRequest(title="Soon", remind_at=now + timedelta(hours=2)))
    for title, start, end in (("A", 30, 90), ("B", 60, 120)):
        service.create_calendar_event(owner, CalendarEventCreateRequest(
            title=title, start_time=now + timedelta(days=1, minutes=start), end_time=now + timedelta(days=1, minutes=end)))
    briefing = build_briefing(service, owner, now=now)
    assert [item.title for item in briefing.reminders_next_24h] == ["Soon"] and briefing.truncated is False
    assert len(briefing.conflicts_next_7_days) == 1 and briefing.conflicts_next_7_days[0].overlap_minutes == 30
