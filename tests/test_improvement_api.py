"""API integration tests for the v3.9 Controlled Self-Improvement & Hardening routes.

Tests /improvement/* endpoints with FastAPI TestClient:
- Authentication & Capability enforcement (401, 403)
- Proposal creation with automated unified diff generation
- Immutable security path enforcement (422)
- Proposal retrieval, listing, and approval decision
- 1-click snapshot restore
- Offline benchmark evaluations
- 404 error handling
"""

from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from approval_flow import approve_over_http
from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy

TEST_TOKEN = "improvement-test-token-at-least-32-chars"
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
    res = no_auth_client.get("/improvement/proposals")
    assert res.status_code == 401

    res = no_auth_client.post(
        "/improvement/proposals",
        json={"target_name": "prompts/test.txt", "proposed_content": "hello", "rationale": "testing unauthenticated"},
    )
    assert res.status_code == 401


def test_capability_enforcement(client):
    client.app_ref.state.authorization_policy = DenyPolicy({Capability.IMPROVEMENT_PROPOSE})

    # Read should succeed
    res = client.get("/improvement/proposals")
    assert res.status_code == 200

    # Propose mutation should fail (403)
    res = client.post(
        "/improvement/proposals",
        json={
            "target_name": "prompts/assistant_persona.md",
            "proposed_content": "new text",
            "rationale": "testing denied capability",
        },
    )
    assert res.status_code == 403


# ===================================================================
# 2. Immutable Security Protection via API (422)
# ===================================================================

def test_immutable_security_path_rejection_api(client):
    payload = {
        "target_name": "jarvis/security/policy.py",
        "proposed_content": "# malicious",
        "rationale": "Attempting to bypass security policies",
    }
    res = client.post("/improvement/proposals", json=payload)
    assert res.status_code == 422
    assert "immutable security file" in res.json()["detail"].lower()


# ===================================================================
# 3. Proposal Lifecycle, Diffing, Approval & Snapshot Restore
# ===================================================================

def test_proposal_lifecycle_and_restore_api(client):
    target = "prompts/assistant_persona.md"
    # v4.9: the current rule line ("secure") must be kept for the proposal to pass its evaluation.
    proposed = "You are JARVIS, a helpful, secure AI personal assistant.\nAlways prioritize user security."

    # Propose mutation
    payload = {
        "target_name": target,
        "proposed_content": proposed,
        "rationale": "Elevate tone and reinforce security priority",
    }
    create_res = client.post("/improvement/proposals", json=payload)
    assert create_res.status_code == 201
    prop = create_res.json()
    prop_id = prop["id"]
    assert prop["status"] == "pending"
    assert "diff" in prop
    assert "+Always prioritize user security." in prop["diff"]

    # List proposals
    list_res = client.get("/improvement/proposals")
    assert list_res.status_code == 200
    page = list_res.json()
    assert page["total"] >= 1
    assert any(p["id"] == prop_id for p in page["items"])

    # Retrieve proposal
    get_res = client.get(f"/improvement/proposals/{prop_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == prop_id

    # Test approval permission requirement
    client.app_ref.state.authorization_policy = DenyPolicy({Capability.IMPROVEMENT_APPROVE})
    denied_decision = client.post(
        f"/improvement/proposals/{prop_id}/decide",
        json={"decision": "approved", "comment": "Approved by reviewer"},
    )
    assert denied_decision.status_code == 403

    # Restore authorization and approve
    client.app_ref.state.authorization_policy = CapabilityPolicy(TEST_OWNER)
    approve_res = approve_over_http(client, lambda approval_id: client.post(
        f"/improvement/proposals/{prop_id}/decide",
        json={"decision": "approved", "comment": "Verified and approved by owner."},
        params={"approval_id": approval_id} if approval_id else {},
    ))
    assert approve_res.status_code == 200
    approved_prop = approve_res.json()
    assert approved_prop["status"] == "approved"
    assert approved_prop["snapshot_id"] is not None

    # Check updated content in service
    svc = client.app_ref.state.improvement_service
    assert svc.get_target_content(target) == proposed

    # Test 1-click snapshot restore
    restore_res = approve_over_http(client, lambda approval_id: client.post(
        f"/improvement/proposals/{prop_id}/restore", params={"approval_id": approval_id} if approval_id else {}))
    assert restore_res.status_code == 200
    restored_prop = restore_res.json()
    assert restored_prop["status"] == "restored"
    assert svc.get_target_content(target) == "You are JARVIS, a helpful, secure AI personal assistant."


# ===================================================================
# 4. Offline Benchmark Evaluations API
# ===================================================================

def test_benchmark_evaluation_api(client):
    res = client.post("/improvement/benchmarks/run")
    assert res.status_code == 200
    bench = res.json()
    assert bench["total_benchmarks"] >= 3
    assert bench["passed_benchmarks"] >= 1
    assert "metrics" in bench
    metric_names = [m["name"] for m in bench["metrics"]]
    assert "instruction_adherence" in metric_names


# ===================================================================
# 5. 404 Error Handling
# ===================================================================

def test_nonexistent_proposal_returns_404(client):
    nonexistent_id = uuid4()
    res = client.get(f"/improvement/proposals/{nonexistent_id}")
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()

    decide_res = client.post(
        f"/improvement/proposals/{nonexistent_id}/decide",
        json={"decision": "approved", "comment": "test"},
    )
    assert decide_res.status_code == 404
