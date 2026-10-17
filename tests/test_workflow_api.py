"""API integration tests for the v3.2 Workflow Orchestration routes.

Tests /workflows/* endpoints with FastAPI TestClient:
- Authentication & Capability enforcement (401, 403)
- Workflow definitions CRUD
- Triggering workflow run and inspecting execution results
- Workflow cancellation
- 404 and 422 validations
"""

from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy

TEST_TOKEN = "workflow-test-token-at-least-32-chars"
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
# 1. Authentication & Security
# ===================================================================

def test_unauthenticated_requests_fail(client):
    no_auth_client = TestClient(client.app_ref)
    res = no_auth_client.get("/workflows/definitions")
    assert res.status_code == 401

    res = no_auth_client.post(
        "/workflows/definitions",
        json={"name": "Unauthorized", "dag_spec": {"steps": [{"step_id": "a", "name": "A"}]}},
    )
    assert res.status_code == 401


def test_capability_enforcement(client):
    client.app_ref.state.authorization_policy = DenyPolicy({Capability.WORKFLOW_WRITE})

    # Read should succeed
    res = client.get("/workflows/definitions")
    assert res.status_code == 200

    # Write should fail (403)
    res = client.post(
        "/workflows/definitions",
        json={
            "name": "Forbidden",
            "dag_spec": {"steps": [{"step_id": "step1", "name": "Step 1"}]},
        },
    )
    assert res.status_code == 403


# ===================================================================
# 2. Workflow Definitions CRUD
# ===================================================================

def test_workflow_definition_crud(client):
    # Create Definition
    payload = {
        "name": "Data Ingestion Pipeline",
        "description": "2-step ingest and notify",
        "dag_spec": {
            "steps": [
                {"step_id": "fetch", "name": "Fetch Source", "step_type": "noop"},
                {
                    "step_id": "process",
                    "name": "Process Data",
                    "step_type": "transform",
                    "depends_on": ["fetch"],
                },
            ]
        },
    }
    create_res = client.post("/workflows/definitions", json=payload)
    assert create_res.status_code == 201
    defn = create_res.json()
    defn_id = defn["id"]
    assert defn["name"] == "Data Ingestion Pipeline"
    assert len(defn["dag_spec"]["steps"]) == 2

    # Get Definition
    get_res = client.get(f"/workflows/definitions/{defn_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == defn_id

    # List Definitions
    list_res = client.get("/workflows/definitions")
    assert list_res.status_code == 200
    assert list_res.json()["total"] == 1
    assert list_res.json()["items"][0]["id"] == defn_id

    # Delete Definition
    del_res = client.delete(f"/workflows/definitions/{defn_id}")
    assert del_res.status_code == 204

    # Get Deleted Definition -> 404
    assert client.get(f"/workflows/definitions/{defn_id}").status_code == 404
    assert client.delete(f"/workflows/definitions/{defn_id}").status_code == 404


def test_invalid_dag_spec_rejected(client):
    # Cyclic DAG
    payload = {
        "name": "Cyclic DAG",
        "dag_spec": {
            "steps": [
                {"step_id": "a", "name": "A", "depends_on": ["b"]},
                {"step_id": "b", "name": "B", "depends_on": ["a"]},
            ]
        },
    }
    res = client.post("/workflows/definitions", json=payload)
    assert res.status_code == 422


# ===================================================================
# 3. Workflow Execution & Runs
# ===================================================================

def test_workflow_execution_and_inspection(client):
    # Setup definition
    payload = {
        "name": "Execution Test",
        "dag_spec": {
            "steps": [
                {"step_id": "step_1", "name": "Step 1", "step_type": "noop", "parameters": {"param": 42}},
                {"step_id": "step_2", "name": "Step 2", "step_type": "transform", "depends_on": ["step_1"]},
            ]
        },
    }
    create_res = client.post("/workflows/definitions", json=payload)
    assert create_res.status_code == 201
    defn_id = create_res.json()["id"]

    # Trigger run
    trigger_res = client.post(
        f"/workflows/definitions/{defn_id}/run",
        json={"inputs": {"job_id": "abc-123"}},
    )
    assert trigger_res.status_code == 201
    detail = trigger_res.json()
    run_id = detail["run"]["id"]
    assert detail["run"]["status"] == "completed"
    assert len(detail["steps"]) == 2
    assert detail["steps"][0]["status"] == "completed"
    assert detail["steps"][1]["status"] == "completed"

    # List runs
    list_runs_res = client.get("/workflows/runs")
    assert list_runs_res.status_code == 200
    assert list_runs_res.json()["total"] == 1
    assert list_runs_res.json()["items"][0]["id"] == run_id

    # Get run detail
    get_run_res = client.get(f"/workflows/runs/{run_id}")
    assert get_run_res.status_code == 200
    assert get_run_res.json()["run"]["id"] == run_id
    assert len(get_run_res.json()["steps"]) == 2

    # Non-existent run -> 404
    missing_id = uuid4()
    assert client.get(f"/workflows/runs/{missing_id}").status_code == 404
