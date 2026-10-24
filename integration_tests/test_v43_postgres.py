"""v4.3 live PostgreSQL validation: enforced approvals are durable, single-use and constrained (migration 0062)."""

import psycopg
import pytest


FINANCE = {"prompt": "Review my financial runway", "dry_run": False}


def test_approval_survives_restart_and_is_used_once(isolated_postgres):
    with isolated_postgres.client() as client:
        waiting = client.post("/autonomous/execute", json=FINANCE)
        assert waiting.status_code == 202
        approval_id = waiting.json()["approval"]["approval_id"]
        assert waiting.json()["approval"]["durable"] is True
        assert client.post(f"/approvals/{approval_id}/approve", json={}).status_code == 200
    with isolated_postgres.client() as restarted:  # a new app instance reads the decision from PostgreSQL
        ran = restarted.post("/autonomous/execute", json={**FINANCE, "approval_id": approval_id})
        assert ran.status_code == 200 and ran.json()["status"] == "completed"
        assert restarted.post("/autonomous/execute", json={**FINANCE, "approval_id": approval_id}).status_code == 409
        stored = restarted.get(f"/approvals/{approval_id}").json()
        events = restarted.get("/activity", params={"category": "approval", "limit": 50}).json()["events"]
    assert stored["state"] == "approved" and stored["consumed_at"] is not None and stored["execution"] == "single_use"
    assert {"approval_used", "approved_action_succeeded"} <= {item["event_type"] for item in events}


def test_database_refuses_inconsistent_approval_rows(isolated_postgres):
    with isolated_postgres.client() as client:
        approval_id = client.post("/autonomous/execute", json=FINANCE).json()["approval"]["approval_id"]
    with psycopg.connect(isolated_postgres.database_url) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):  # a pending request can never be marked used
            connection.execute("UPDATE jarvis_app.approval_requests SET consumed_at = now() WHERE id = %s",
                               (approval_id,))
    with psycopg.connect(isolated_postgres.database_url) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute("UPDATE jarvis_app.approval_requests SET category = 'unknown' WHERE id = %s",
                               (approval_id,))
