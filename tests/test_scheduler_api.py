"""API integration tests for the v3.5 Proactive Assistance & Scheduling routes.

Tests /scheduler/* endpoints with FastAPI TestClient:
- Authentication & Capability enforcement (401, 403)
- Job CRUD and immediate execution (/scheduler/jobs/{id}/run)
- Proactive notifications dispatch, anti-fatigue gating (429), and acknowledgement
- 404 error handling for missing jobs and notifications
"""

from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy

TEST_TOKEN = "scheduler-test-token-at-least-32-chars"
TEST_OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}"}


class DenyPolicy(CapabilityPolicy):
    def __init__(self, denied):
        super().__init__(TEST_OWNER)
        self.denied = denied

    def allows(self, owner_id, capability, **kwargs):
        return capability not in self.denied and super().allows(owner_id, capability, **kwargs)


def make_app(**overrides):
    settings = Settings(
        _env_file=None,
        environment="test",
        log_level="ERROR",
        local_auth_token=SecretStr(TEST_TOKEN),
        owner_id=TEST_OWNER,
        **overrides,
    )
    return create_app(settings=settings)


@pytest.fixture
def client():
    app = make_app()
    with TestClient(app, headers=AUTH_HEADERS) as c:
        c.app_ref = app
        yield c


# ===================================================================
# 1. Authentication & Capability Enforcement
# ===================================================================

def test_unauthenticated_requests_fail(client):
    no_auth_client = TestClient(client.app_ref)
    res = no_auth_client.get("/scheduler/jobs")
    assert res.status_code == 401

    res = no_auth_client.post(
        "/scheduler/jobs",
        json={"name": "Job", "schedule_type": "interval", "schedule_expr": "60", "action_type": "reminder"},
    )
    assert res.status_code == 401


def test_capability_enforcement(client):
    client.app_ref.state.authorization_policy = DenyPolicy({Capability.SCHEDULER_WRITE})

    # Read should succeed
    res = client.get("/scheduler/jobs")
    assert res.status_code == 200

    # Write should fail (403)
    res = client.post(
        "/scheduler/jobs",
        json={"name": "Job", "schedule_type": "interval", "schedule_expr": "60", "action_type": "reminder"},
    )
    assert res.status_code == 403


# ===================================================================
# 2. Scheduled Job CRUD & Execution
# ===================================================================

def test_job_crud_and_execution(client):
    # Create job
    create_payload = {
        "name": "Hourly Health Check",
        "schedule_type": "interval",
        "schedule_expr": "3600",
        "action_type": "health_check",
        "payload": {"deep": True},
    }
    create_res = client.post("/scheduler/jobs", json=create_payload)
    assert create_res.status_code == 201
    job = create_res.json()
    job_id = job["id"]
    assert job["name"] == "Hourly Health Check"
    assert job["last_status"] == "idle"

    # Get job
    get_res = client.get(f"/scheduler/jobs/{job_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == job_id

    # List jobs
    list_res = client.get("/scheduler/jobs")
    assert list_res.status_code == 200
    assert list_res.json()["total"] == 1

    # Update job
    update_res = client.patch(
        f"/scheduler/jobs/{job_id}",
        json={"name": "Updated Health Check", "schedule_expr": "1800"},
    )
    assert update_res.status_code == 200
    assert update_res.json()["name"] == "Updated Health Check"
    assert update_res.json()["schedule_expr"] == "1800"

    # Run job immediately
    run_res = client.post(f"/scheduler/jobs/{job_id}/run")
    assert run_res.status_code == 200
    run_result = run_res.json()
    assert run_result["status"] == "success"
    assert run_result["output"]["all_healthy"] is True

    # Delete job
    del_res = client.delete(f"/scheduler/jobs/{job_id}")
    assert del_res.status_code == 204

    # 404 after delete
    assert client.get(f"/scheduler/jobs/{job_id}").status_code == 404
    assert client.delete(f"/scheduler/jobs/{job_id}").status_code == 404


# ===================================================================
# 3. Notifications & Anti-Fatigue Throttling
# ===================================================================

def test_notifications_dispatch_and_throttling(client):
    # First notification succeeds
    notif_payload = {
        "category": "workflow",
        "title": "Pipeline Succeeded",
        "message": "All steps executed within budget.",
        "priority": "normal",
    }
    res1 = client.post("/scheduler/notifications", json=notif_payload)
    assert res1.status_code == 201
    notif = res1.json()
    notif_id = notif["id"]
    assert notif["title"] == "Pipeline Succeeded"
    assert notif["read"] is False

    # Second notification in same category within quiet period triggers 429
    res2 = client.post("/scheduler/notifications", json=notif_payload)
    assert res2.status_code == 429

    # Urgent notification bypasses quiet period
    urgent_payload = {
        "category": "workflow",
        "title": "Pipeline Critical Alert",
        "message": "Immediate attention requested.",
        "priority": "urgent",
    }
    res3 = client.post("/scheduler/notifications", json=urgent_payload)
    assert res3.status_code == 201

    # List unread notifications
    list_res = client.get("/scheduler/notifications?unread_only=true")
    assert list_res.status_code == 200
    assert list_res.json()["total"] == 2

    # Mark first notification as read
    read_res = client.patch(f"/scheduler/notifications/{notif_id}/read")
    assert read_res.status_code == 200
    assert read_res.json()["marked"] is True

    # Unread count decrements to 1
    list_unread = client.get("/scheduler/notifications?unread_only=true")
    assert list_unread.json()["total"] == 1
