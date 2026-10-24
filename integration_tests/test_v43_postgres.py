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


def test_concurrent_use_of_one_approval_succeeds_once_in_postgresql(isolated_postgres):
    import threading

    from jarvis.audit.models import ApprovalProposal
    from jarvis.audit.service import ApprovalError, ApprovalService, AuditService
    from jarvis.audit.stores import PostgresApprovalStore, PostgresAuditStore
    from test_postgres_integration import TEST_OWNER_ID

    url = isolated_postgres.database_url
    service = ApprovalService(PostgresApprovalStore(url), AuditService(PostgresAuditStore(url)), ttl_seconds=900)
    proposal = ApprovalProposal(category="quarantine", action="security.quarantine", target="a.txt", reason="r",
                                risk="medium", expected_side_effects="moves a.txt", scope_material="a.txt|hash")
    request = service.request(TEST_OWNER_ID, proposal)
    service.decide(TEST_OWNER_ID, request.approval_id, approve=True, note=None)
    used, refused, barrier = [], [], threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            used.append(service.consume(TEST_OWNER_ID, request.approval_id, proposal).approval_id)
        except ApprovalError as exc:
            refused.append(exc.status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert used == [request.approval_id] and refused == [409] * 7
