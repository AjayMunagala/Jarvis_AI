"""v7.4 live PostgreSQL validation: travel decisions, trips, segments, cascade delete and persisted reminder links."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest


def _service(url):
    from jarvis.assistant.service import AssistantService
    from jarvis.assistant.stores import PostgresAssistantStore
    from jarvis.travel.service import TravelService
    from jarvis.travel.stores import PostgresTravelStore

    return TravelService(PostgresTravelStore(url), assistant_service=AssistantService(PostgresAssistantStore(url)))


def _trip(**extra):
    from jarvis.travel.models import TripCreateRequest

    return TripCreateRequest.model_validate({
        "name": "Delhi to London", "travellers": 2, "currency": "INR", "budget": "250000.5", "start_date": "2027-11-01",
        "end_date": "2027-11-05", "home_timezone": "Asia/Kolkata", "daily_food": "3000", **extra})


def _segment(**extra):
    from jarvis.travel.models import SegmentCreateRequest

    return SegmentCreateRequest.model_validate({
        "kind": "flight", "title": "AI 161", "from_place": "Delhi", "to_place": "London",
        "start_local": "2027-11-01T23:55", "start_timezone": "Asia/Kolkata", "end_local": "2027-11-02T05:30",
        "end_timezone": "Europe/London", "reference": "PNR-XY12", "cost_amount": "45000.75", **extra})


def test_decisions_round_trip_and_persist(isolated_postgres):
    from jarvis.travel.models import DecisionCreateRequest, DecisionUpdateRequest
    from jarvis.travel.service import TravelNotFound

    url, owner, other = isolated_postgres.database_url, uuid4(), uuid4()
    service = _service(url)
    created = service.create_decision(owner, DecisionCreateRequest.model_validate({
        "question": "Train or flight?", "notes": "Prices from my notes",
        "options": [{"name": "Train", "values": {"price": 120, "hours": 6}},
                    {"name": "Flight", "values": {"price": 200, "hours": None}, "notes": "early"}],
        "criteria": [{"name": "price", "weight": 60, "direction": "lower_better", "unit": "EUR"},
                     {"name": "hours", "weight": 40, "direction": "lower_better"}]}))
    fresh = _service(url)  # a new store instance reads what the first one wrote
    assert fresh.get_decision(owner, created.id) == created
    assert fresh.decision_result(owner, created.id).winners == ["Train"]
    with pytest.raises(TravelNotFound):
        fresh.get_decision(other, created.id)
    updated = fresh.update_decision(owner, created.id, DecisionUpdateRequest(question="How to go?", notes=None))
    assert _service(url).get_decision(owner, created.id) == updated and updated.notes is None
    assert fresh.list_decisions(owner).total == 1 and fresh.list_decisions(other).total == 0
    fresh.delete_decision(owner, created.id)
    assert fresh.list_decisions(owner).total == 0


def test_trips_segments_reminder_links_and_cascade(isolated_postgres):
    import psycopg

    from jarvis.travel.models import SegmentUpdateRequest, TripUpdateRequest
    from jarvis.travel.service import TravelNotFound

    url, owner, other = isolated_postgres.database_url, uuid4(), uuid4()
    service = _service(url)
    trip = service.create_trip(owner, _trip())
    flight = service.create_segment(owner, trip.id, _segment())
    stay = service.create_segment(owner, trip.id, _segment(kind="stay", title="Hotel", from_place="London",
                                                           to_place=None, start_local="2027-11-02T14:00",
                                                           start_timezone="Europe/London", end_local="2027-11-05T11:00",
                                                           end_timezone=None, reference=None, cost_amount="60000"))
    fresh = _service(url)
    assert fresh.get_trip(owner, trip.id) == trip and fresh.get_trip(owner, trip.id).budget == Decimal("250000.5")
    stored = fresh.get_segment(owner, trip.id, flight.id)
    assert stored == flight and stored.reference == "PNR-XY12" and stored.cost_currency == "INR"
    assert stored.start_utc == datetime(2027, 11, 1, 18, 25, tzinfo=timezone.utc)
    assert [item.id for item in fresh.list_segments(owner, trip.id).items] == [flight.id, stay.id]
    with pytest.raises(TravelNotFound):
        fresh.get_trip(other, trip.id)
    summary = fresh.summary(owner, trip.id)
    assert summary.timeline[0].duration_minutes == 665 and summary.cost.total == Decimal("120000.75")
    assert [gap.night_of.isoformat() for gap in summary.gaps] == ["2027-11-01"]
    assert fresh.update_trip(owner, trip.id, TripUpdateRequest(travellers=3)).travellers == 3

    first = fresh.sync_reminders(owner, trip.id)
    assert [item.action for item in first.items] == ["created"]
    links = _service(url).store.list_links(owner, trip.id)
    assert [(link.segment_id, link.reminder_id, link.lead_minutes) for link in links] == [
        (flight.id, first.items[0].reminder_id, 180)]
    assert [item.action for item in _service(url).sync_reminders(owner, trip.id).items] == ["unchanged"]
    assert fresh.assistant_service.list_reminders(owner).total == 1
    fresh.update_segment(owner, trip.id, flight.id, SegmentUpdateRequest(start_local="2027-11-01T22:55"))
    reminder = fresh.assistant_service.get_reminder(owner, first.items[0].reminder_id)
    assert reminder.remind_at == datetime(2027, 11, 1, 14, 25, tzinfo=timezone.utc)  # 17:25 UTC minus 3 h
    assert _service(url).store.get_link(owner, flight.id).remind_at == reminder.remind_at

    with psycopg.connect(url) as conn:
        counts = lambda: [conn.execute(f"SELECT count(*) FROM jarvis_app.{table} WHERE owner_id = %s",  # noqa: E731
                                       (owner,)).fetchone()[0]
                          for table in ("travel_trips", "travel_segments", "travel_reminder_links")]
        assert counts() == [1, 2, 1]
        conn.commit()
        fresh.delete_trip(owner, trip.id)
        assert counts() == [0, 0, 0]  # segments and links cascade with the trip
    assert fresh.assistant_service.list_reminders(owner).total == 0  # linked reminders were deleted first


def test_segment_cap_and_cascade_from_a_segment(isolated_postgres, monkeypatch):
    from jarvis.travel import service as module
    from jarvis.travel.service import TravelConflict

    monkeypatch.setattr(module, "MAX_SEGMENTS_PER_TRIP", 2)
    url, owner = isolated_postgres.database_url, uuid4()
    service = _service(url)
    trip = service.create_trip(owner, _trip())
    first = service.create_segment(owner, trip.id, _segment())
    service.create_segment(owner, trip.id, _segment(kind="train", start_local="2027-11-03T10:00",
                                                    start_timezone="Europe/London", end_local="2027-11-03T12:00",
                                                    end_timezone=None))
    with pytest.raises(TravelConflict):
        service.create_segment(owner, trip.id, _segment())
    service.sync_reminders(owner, trip.id)
    assert len(service.store.list_links(owner, trip.id)) == 2
    service.delete_segment(owner, trip.id, first.id)
    assert [link.segment_id for link in service.store.list_links(owner, trip.id)] != [first.id]
    assert len(service.store.list_links(owner, trip.id)) == 1
    assert service.assistant_service.list_reminders(owner).total == 1
