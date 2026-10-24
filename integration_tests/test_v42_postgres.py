"""v4.2 live PostgreSQL validation: categories (0060), nullable telemetry (0061), durable failure audit, status."""

from datetime import datetime, timezone
from uuid import uuid4

from test_postgres_integration import TEST_OWNER_ID


def test_status_runs_a_real_database_check(isolated_postgres):
    with isolated_postgres.client() as client:
        report = client.get("/status").json()
    database = next(item for item in report["subsystems"] if item["key"] == "database")
    assert database["state"] == "operational" and database["checks"][0].startswith("SELECT 1 succeeded")
    assert next(item for item in report["subsystems"] if item["key"] == "assistant")["storage"] == "postgresql"


def test_subsystem_categories_and_failures_are_stored_durably(isolated_postgres):
    with isolated_postgres.client() as client:
        assert client.post("/assistant/tasks", json={"title": "Durable audit"},
                           headers={"X-Request-ID": "trace-live-ok"}).status_code == 201
        assert client.post("/finance/accounts", json={"name": ""}, headers={"X-Request-ID": "trace-live-fail"}).status_code == 422
    with isolated_postgres.client() as restarted:
        page = restarted.get("/activity", params={"limit": 100}).json()
    assert page["durable_storage"] is True
    created = next(item for item in page["events"] if item["correlation_id"] == "trace-live-ok")
    failed = next(item for item in page["events"] if item["correlation_id"] == "trace-live-fail")
    assert created["category"] == "assistant" and created["durable"] is True
    assert (failed["category"], failed["event_type"], failed["status"]) == ("finance", "request_failed", "failed")


def test_unmeasured_telemetry_is_stored_as_null_not_zero(isolated_postgres):
    from jarvis.health.models import HealthSnapshot
    from jarvis.health.stores import PostgresHealthStore

    store = PostgresHealthStore(isolated_postgres.database_url)
    snapshot = HealthSnapshot(id=uuid4(), owner_id=TEST_OWNER_ID, cpu_percent=None, memory_percent=None,
                              disk_free_gb=None, services_status={"status": "unavailable"},
                              created_at=datetime.now(timezone.utc))
    store.save_snapshot(snapshot)
    items, _ = store.list_snapshots(TEST_OWNER_ID, limit=50, offset=0)
    stored = next(item for item in items if item.id == snapshot.id)
    assert (stored.cpu_percent, stored.memory_percent, stored.disk_free_gb) == (None, None, None)
    with isolated_postgres.client() as client:
        measured = client.post("/health/diagnostic/snapshots").json()
    assert measured["services_status"]["database"] == "operational"
