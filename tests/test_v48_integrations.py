"""v4.8 integration and device readiness: adapters, enrollments, approvals, revocation, secret references."""

import json
import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from approval_flow import approve_over_http
from jarvis.audit.models import ApprovalCategory, AuditCategory
from jarvis.core.config import Settings
from jarvis.main import create_app

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TOKEN = "v48-integrations-token-with-32-characters"
MIGRATION = Path(__file__).resolve().parents[1] / "database" / "scripts" / "0065_integrations.sql"
SECRET_VALUE = "s3cr3t-lamp-token-never-shown"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("JARVIS_INTEGRATION_LAMP", SECRET_VALUE)
    app = create_app(settings=Settings(_env_file=None, environment="test", log_level="ERROR",
                                       local_auth_token=SecretStr(TOKEN), owner_id=OWNER))
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as test_client:
        test_client.app_ref = app
        yield test_client


def enroll(client, **extra):
    response = client.post("/integrations/enrollments",
                           json={"adapter_id": "mock.lamp", "display_name": "Desk lamp", **extra})
    assert response.status_code == 201, response.text
    return response.json()["enrollment"]["enrollment_id"]


def activate(client, enrollment_id):
    url = f"/integrations/enrollments/{enrollment_id}/activate"
    return approve_over_http(client, lambda approval_id: client.post(
        url, params={"approval_id": approval_id} if approval_id else {}))


def act(client, enrollment_id, action, arguments=None, approval_id=None):
    body = {"action": action, "arguments": arguments or {}}
    if approval_id:
        body["approval_id"] = approval_id
    return client.post(f"/integrations/enrollments/{enrollment_id}/actions", json=body)


def test_only_the_mock_adapter_exists(client):
    adapters = client.get("/integrations/adapters").json()
    assert [item["adapter_id"] for item in adapters] == ["mock.lamp"] and adapters[0]["mode"] == "mock"
    integrations = next(item for item in client.get("/status").json()["subsystems"] if item["key"] == "integrations")
    assert integrations["state"] == "mock" and integrations["storage"] == "memory"


def test_enrollment_needs_approval_to_activate_and_does_nothing_while_pending(client):
    enrollment_id = enroll(client)
    assert act(client, enrollment_id, "read_state").status_code == 409  # pending: nothing runs
    waiting = client.post(f"/integrations/enrollments/{enrollment_id}/activate")
    assert waiting.status_code == 202 and waiting.json()["approval"]["category"] == "integration"
    assert client.get(f"/integrations/enrollments/{enrollment_id}").json()["enrollment"]["state"] == "pending"
    activated = activate(client, enrollment_id)
    assert activated.status_code == 200 and activated.json()["enrollment"]["state"] == "active"
    again = client.post(f"/integrations/enrollments/{enrollment_id}/activate")
    assert again.status_code == 409  # only a pending enrollment can be activated


def test_read_only_actions_run_directly_and_changes_need_an_exact_approval(client):
    enrollment_id = enroll(client)
    activate(client, enrollment_id)
    state = act(client, enrollment_id, "read_state")
    assert state.status_code == 200 and state.json()["output"] == {"power": "off", "brightness": 0, "simulated": True}
    waiting = act(client, enrollment_id, "set_power", {"on": True})
    assert waiting.status_code == 202
    approval_id = waiting.json()["approval"]["approval_id"]
    assert act(client, enrollment_id, "read_state").json()["output"]["power"] == "off"  # nothing ran yet
    client.post(f"/approvals/{approval_id}/approve", json={})
    assert act(client, enrollment_id, "set_power", {"on": False}, approval_id).status_code == 409  # other arguments
    done = act(client, enrollment_id, "set_power", {"on": True}, approval_id)
    assert done.status_code == 200 and done.json()["output"]["power"] == "on"
    assert act(client, enrollment_id, "set_power", {"on": True}, approval_id).status_code == 409  # single use


def test_revoke_is_immediate_final_and_beats_an_approved_action(client):
    enrollment_id = enroll(client)
    activate(client, enrollment_id)
    approval_id = act(client, enrollment_id, "set_brightness", {"level": 70}).json()["approval"]["approval_id"]
    client.post(f"/approvals/{approval_id}/approve", json={})
    revoked = client.post(f"/integrations/enrollments/{enrollment_id}/revoke")
    assert revoked.status_code == 200 and revoked.json()["enrollment"]["state"] == "revoked"
    assert act(client, enrollment_id, "set_brightness", {"level": 70}, approval_id).status_code == 409
    assert act(client, enrollment_id, "read_state").status_code == 409
    assert client.post(f"/integrations/enrollments/{enrollment_id}/activate").status_code == 409  # never again
    assert client.post(f"/integrations/enrollments/{enrollment_id}/revoke").status_code == 200  # idempotent


def test_secret_references_never_expose_the_value(client):
    enrollment_id = enroll(client, secret_ref="env:JARVIS_INTEGRATION_LAMP")
    missing = enroll(client, secret_ref="env:JARVIS_INTEGRATION_ABSENT")
    view = client.get(f"/integrations/enrollments/{enrollment_id}").json()
    assert view["secret_status"] == "present" and view["enrollment"]["secret_ref"] == "env:JARVIS_INTEGRATION_LAMP"
    assert client.get(f"/integrations/enrollments/{missing}").json()["secret_status"] == "missing"
    assert client.post(f"/integrations/enrollments/{missing}/activate").status_code == 422
    activate(client, enrollment_id)
    act(client, enrollment_id, "read_state")
    everything = json.dumps([client.get("/integrations/enrollments").json(),
                             client.get("/activity", params={"limit": 200}).json(),
                             client.get("/approvals").json()])
    assert SECRET_VALUE not in everything
    for bad in ("env:PATH", "env:JARVIS_INTEGRATION_lower", "file:/etc/passwd", "JARVIS_INTEGRATION_X"):
        response = client.post("/integrations/enrollments",
                               json={"adapter_id": "mock.lamp", "display_name": "x", "secret_ref": bad})
        assert response.status_code == 422, bad


def test_invalid_actions_and_arguments_are_refused(client):
    enrollment_id = enroll(client)
    activate(client, enrollment_id)
    for action, arguments in (("unlock_door", {}), ("set_power", {"on": "yes"}), ("set_brightness", {"level": 101}),
                              ("set_brightness", {"level": True}), ("read_state", {"x": 1})):
        assert act(client, enrollment_id, action, arguments).status_code == 422, (action, arguments)
    assert client.post("/integrations/enrollments", json={"adapter_id": "real.lock", "display_name": "x"}).status_code == 422
    assert client.get("/integrations/enrollments/EN-0000000000000000").status_code == 404
    assert client.get("/integrations/enrollments/not-an-id").status_code == 422


def test_enrollments_are_owner_scoped(client):
    from jarvis.integrations.models import ActionRequest
    from jarvis.integrations.service import IntegrationError

    enrollment_id = enroll(client)
    activate(client, enrollment_id)
    service = client.app_ref.state.integration_service
    other = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    assert service.list_enrollments(other).enrollments == ()
    for call in (lambda: service.get_enrollment(other, enrollment_id),
                 lambda: service.activate(other, enrollment_id),
                 lambda: service.revoke(other, enrollment_id),
                 lambda: service.act(other, enrollment_id, ActionRequest(action="read_state"))):
        with pytest.raises(IntegrationError) as refused:
            call()
        assert refused.value.status_code == 404
    assert client.get(f"/integrations/enrollments/{enrollment_id}").json()["enrollment"]["state"] == "active"


def test_an_approval_for_one_enrollment_or_action_does_not_cover_another(client):
    first, second = enroll(client), enroll(client)
    activate(client, first)
    activate(client, second)
    approval_id = act(client, first, "set_power", {"on": True}).json()["approval"]["approval_id"]
    client.post(f"/approvals/{approval_id}/approve", json={})
    assert act(client, second, "set_power", {"on": True}, approval_id).status_code == 409
    assert act(client, first, "set_brightness", {"level": 1}, approval_id).status_code == 409
    assert act(client, first, "set_power", {"on": True}, approval_id).status_code == 200


def test_a_revoke_waits_for_an_in_flight_action_and_wins_afterwards(client):
    import threading

    enrollment_id = enroll(client)
    activate(client, enrollment_id)
    service = client.app_ref.state.integration_service
    adapter = service.adapters["mock.lamp"]
    started, release, revoked = threading.Event(), threading.Event(), threading.Event()
    original = adapter.execute

    def slow_execute(*args):
        started.set()
        release.wait(5)
        return original(*args)

    adapter.execute = slow_execute
    worker = threading.Thread(target=lambda: act(client, enrollment_id, "read_state"))
    worker.start()
    assert started.wait(5)
    revoker = threading.Thread(target=lambda: (service.revoke(OWNER, enrollment_id), revoked.set()))
    revoker.start()
    assert not revoked.wait(0.3)  # the revoke waits while the action runs
    release.set()
    worker.join()
    revoker.join()
    assert revoked.is_set() and enrollment_id not in adapter._state  # forgotten after the action finished
    adapter.execute = original
    assert act(client, enrollment_id, "read_state").status_code == 409


def test_migration_0065_matches_the_models():
    sql = MIGRATION.read_text(encoding="utf-8")
    audit = set(re.search(r"audit_events_category_allowed\s+CHECK \(category IN \(([^)]*)\)\)", sql)
                .group(1).replace("'", "").replace(" ", "").replace("\n", "").split(","))
    approval = set(re.search(r"approval_requests_category_allowed\s+CHECK \(category IN \(([^)]*)\)\)", sql)
                   .group(1).replace("'", "").replace(" ", "").split(","))
    # v6.5 (migration 0068) later adds the 'tutor' audit category.
    assert audit == set(AuditCategory.__args__) - {"tutor"} and approval == set(ApprovalCategory.__args__)
    assert "DROP TABLE" not in sql.upper()
