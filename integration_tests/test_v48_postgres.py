"""v4.8 live PostgreSQL validation: durable enrollments, approved activation, final revocation, database checks."""

import psycopg
import pytest


def test_enrollment_lifecycle_is_durable_and_revocation_is_final(isolated_postgres):
    with isolated_postgres.client() as client:
        enrollment = client.post("/integrations/enrollments",
                                 json={"adapter_id": "mock.lamp", "display_name": "Desk lamp"}).json()["enrollment"]
        enrollment_id = enrollment["enrollment_id"]
        assert enrollment["durable"] is True
        url = f"/integrations/enrollments/{enrollment_id}/activate"
        approval_id = client.post(url).json()["approval"]["approval_id"]
        client.post(f"/approvals/{approval_id}/approve", json={})
        assert client.post(url, params={"approval_id": approval_id}).json()["enrollment"]["state"] == "active"
    with isolated_postgres.client() as restarted:
        assert restarted.get(f"/integrations/enrollments/{enrollment_id}").json()["enrollment"]["state"] == "active"
        assert restarted.post(f"/integrations/enrollments/{enrollment_id}/actions",
                              json={"action": "read_state"}).status_code == 200
        assert restarted.post(f"/integrations/enrollments/{enrollment_id}/revoke").json()["enrollment"]["state"] == "revoked"
        events = restarted.get("/activity", params={"category": "integration", "limit": 50}).json()["events"]
    assert {"integration.enroll", "integration.activate", "integration.revoke"} <= {e["event_type"] for e in events}
    with psycopg.connect(isolated_postgres.database_url) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):  # a revoked row cannot claim to be active without a time
            connection.execute("UPDATE jarvis_app.integration_enrollments SET state = 'active', activated_at = NULL "
                               "WHERE id = %s", (enrollment_id,))
    with psycopg.connect(isolated_postgres.database_url) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):  # only env: references to JARVIS_INTEGRATION_ variables
            connection.execute("UPDATE jarvis_app.integration_enrollments SET secret_ref = 'env:PATH' WHERE id = %s",
                               (enrollment_id,))
