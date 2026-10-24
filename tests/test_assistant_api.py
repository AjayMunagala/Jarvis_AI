"""API integration tests for the v3.1 Personal Assistant routes.

Tests /assistant/* endpoints with FastAPI TestClient:
- Authentication & Capability enforcement (401, 403)
- Task CRUD, filtering, pagination
- Note CRUD, search, tag filtering
- Reminder CRUD and status transitions
- Calendar event scheduling and range queries
- 404 and 422 validations
"""

from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy

TEST_TOKEN = "assistant-test-token-at-least-32-chars"
TEST_OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_OWNER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
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
# 1. Authentication & Security
# ===================================================================

def test_unauthenticated_requests_fail(client):
    no_auth_client = TestClient(client.app_ref)
    res = no_auth_client.get("/assistant/tasks")
    assert res.status_code == 401

    res = no_auth_client.post("/assistant/tasks", json={"title": "Unauthorized"})
    assert res.status_code == 401


def test_capability_enforcement(client):
    client.app_ref.state.authorization_policy = DenyPolicy({Capability.ASSISTANT_WRITE})

    # Read should succeed
    res = client.get("/assistant/tasks")
    assert res.status_code == 200

    # Write should be forbidden (403)
    res = client.post("/assistant/tasks", json={"title": "Should Fail"})
    assert res.status_code == 403


# ===================================================================
# 2. Task Endpoints
# ===================================================================

def test_task_crud_flow(client):
    now = datetime.now(timezone.utc)
    due = (now + timedelta(days=2)).isoformat()

    # Create Task
    create_payload = {
        "title": "Build v3.1 Subsystem",
        "description": "Implement personal assistant capabilities",
        "priority": "high",
        "due_date": due,
        "tags": ["Engineering", "Release"],
    }
    create_res = client.post("/assistant/tasks", json=create_payload)
    assert create_res.status_code == 201
    task = create_res.json()
    task_id = task["id"]
    assert task["title"] == "Build v3.1 Subsystem"
    assert task["priority"] == "high"
    assert task["status"] == "pending"
    assert task["tags"] == ["engineering", "release"]

    # Get Task
    get_res = client.get(f"/assistant/tasks/{task_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == task_id

    # List Tasks
    list_res = client.get("/assistant/tasks", params={"tag": "engineering", "priority": "high"})
    assert list_res.status_code == 200
    data = list_res.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == task_id

    # Update Task
    update_res = client.patch(
        f"/assistant/tasks/{task_id}",
        json={"status": "completed", "priority": "medium"},
    )
    assert update_res.status_code == 200
    assert update_res.json()["status"] == "completed"
    assert update_res.json()["priority"] == "medium"

    # Delete Task
    del_res = client.delete(f"/assistant/tasks/{task_id}")
    assert del_res.status_code == 204

    # Get Deleted Task -> 404
    get_del_res = client.get(f"/assistant/tasks/{task_id}")
    assert get_del_res.status_code == 404

    # Delete again -> 404
    del_again_res = client.delete(f"/assistant/tasks/{task_id}")
    assert del_again_res.status_code == 404


def test_task_invalid_inputs(client):
    # Empty title or missing required field
    res = client.post("/assistant/tasks", json={})
    assert res.status_code == 422

    # Invalid priority
    res = client.post("/assistant/tasks", json={"title": "Bad Priority", "priority": "critical_extreme"})
    assert res.status_code == 422


# ===================================================================
# 3. Note Endpoints & Search
# ===================================================================

def test_note_crud_and_search(client):
    # Create notes
    res1 = client.post(
        "/assistant/notes",
        json={"title": "Design Specs", "content": "Architecture overview of autonomous OS", "tags": ["design", "arch"]},
    )
    assert res1.status_code == 201
    n1_id = res1.json()["id"]

    res2 = client.post(
        "/assistant/notes",
        json={"title": "Grocery List", "content": "Apples, Oranges, Tea", "tags": ["home"]},
    )
    assert res2.status_code == 201
    n2_id = res2.json()["id"]

    # List notes
    list_res = client.get("/assistant/notes")
    assert list_res.status_code == 200
    assert list_res.json()["total"] == 2

    # Search notes via query
    search_res = client.get("/assistant/notes", params={"query": "architecture"})
    assert search_res.status_code == 200
    data = search_res.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == n1_id

    # Update note
    upd_res = client.patch(f"/assistant/notes/{n1_id}", json={"content": "Updated arch details"})
    assert upd_res.status_code == 200
    assert upd_res.json()["content"] == "Updated arch details"

    # Delete note
    del_res = client.delete(f"/assistant/notes/{n2_id}")
    assert del_res.status_code == 204

    # Confirm deleted
    assert client.get(f"/assistant/notes/{n2_id}").status_code == 404


# ===================================================================
# 4. Reminder Endpoints
# ===================================================================

def test_reminder_crud(client):
    now = datetime.now(timezone.utc)
    due = (now + timedelta(hours=1)).isoformat()

    # Create reminder
    res = client.post(
        "/assistant/reminders",
        json={"title": "Standup call", "remind_at": due},
    )
    assert res.status_code == 201
    rem_id = res.json()["id"]
    assert res.json()["status"] == "pending"

    # List reminders
    list_res = client.get("/assistant/reminders", params={"status": "pending"})
    assert list_res.status_code == 200
    assert list_res.json()["total"] == 1

    # Update status to dismissed
    upd_res = client.patch(f"/assistant/reminders/{rem_id}", json={"status": "dismissed"})
    assert upd_res.status_code == 200
    assert upd_res.json()["status"] == "dismissed"

    # Delete reminder
    del_res = client.delete(f"/assistant/reminders/{rem_id}")
    assert del_res.status_code == 204
    assert client.delete(f"/assistant/reminders/{rem_id}").status_code == 404


# ===================================================================
# 5. Calendar Endpoints
# ===================================================================

def test_calendar_endpoints(client):
    now = datetime.now(timezone.utc)
    start = (now + timedelta(hours=3)).isoformat()
    end = (now + timedelta(hours=4)).isoformat()

    # Create event
    res = client.post(
        "/assistant/calendar/events",
        json={"title": "Architecture Review", "start_time": start, "end_time": end, "location": "Online"},
    )
    assert res.status_code == 201
    ev_id = res.json()["id"]

    # Query range using params dictionary to ensure proper URL encoding
    query_start = (now + timedelta(hours=2)).isoformat()
    query_end = (now + timedelta(hours=5)).isoformat()
    list_res = client.get("/assistant/calendar/events", params={"start_range": query_start, "end_range": query_end})
    assert list_res.status_code == 200
    data = list_res.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == ev_id

    # Delete event
    del_res = client.delete(f"/assistant/calendar/events/{ev_id}")
    assert del_res.status_code == 204
    assert client.delete(f"/assistant/calendar/events/{ev_id}").status_code == 404
