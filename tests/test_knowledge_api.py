"""API integration tests for the v3.3 Knowledge Intelligence routes.

Tests /knowledge/* endpoints with FastAPI TestClient:
- Authentication & Capability enforcement (401, 403)
- Document ingestion, listing, detail, deletion
- Hybrid search (RRF scoring)
- Context budget allocation
- 404 and 422 validations
"""

from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy

TEST_TOKEN = "knowledge-test-token-at-least-32-chars"
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
    res = no_auth_client.get("/knowledge/documents")
    assert res.status_code == 401

    res = no_auth_client.post(
        "/knowledge/documents",
        json={"title": "Doc", "content": "Sample content"},
    )
    assert res.status_code == 401


def test_capability_enforcement(client):
    client.app_ref.state.authorization_policy = DenyPolicy({Capability.KNOWLEDGE_WRITE})

    # Read should succeed
    res = client.get("/knowledge/documents")
    assert res.status_code == 200

    # Write should fail (403)
    res = client.post(
        "/knowledge/documents",
        json={"title": "Forbidden", "content": "Secret text"},
    )
    assert res.status_code == 403


# ===================================================================
# 2. Document Ingestion & Retrieval
# ===================================================================

def test_knowledge_document_crud(client):
    # Ingest Document
    payload = {
        "title": "Autonomous Operating Systems",
        "content": "A detailed manual on building resilient agentic systems.",
        "source_type": "manual",
    }
    create_res = client.post("/knowledge/documents", json=payload)
    assert create_res.status_code == 201
    detail = create_res.json()
    doc_id = detail["document"]["id"]
    assert detail["document"]["title"] == "Autonomous Operating Systems"
    assert len(detail["chunks"]) > 0

    # Get Document
    get_res = client.get(f"/knowledge/documents/{doc_id}")
    assert get_res.status_code == 200
    assert get_res.json()["document"]["id"] == doc_id

    # List Documents
    list_res = client.get("/knowledge/documents")
    assert list_res.status_code == 200
    assert list_res.json()["total"] == 1
    assert list_res.json()["items"][0]["id"] == doc_id

    # Delete Document
    del_res = client.delete(f"/knowledge/documents/{doc_id}")
    assert del_res.status_code == 204

    # 404 on deleted
    assert client.get(f"/knowledge/documents/{doc_id}").status_code == 404
    assert client.delete(f"/knowledge/documents/{doc_id}").status_code == 404


# ===================================================================
# 3. Hybrid Search & Context Budget
# ===================================================================

def test_search_and_context_budget_api(client):
    # Ingest document for search
    doc_payload = {
        "title": "Quantum Computing Principles",
        "content": "Quantum superposition and entanglement enable quantum speedup in specialized algorithms.",
    }
    create_res = client.post("/knowledge/documents", json=doc_payload)
    assert create_res.status_code == 201
    doc_id = create_res.json()["document"]["id"]

    # Hybrid Search
    search_payload = {"query": "superposition entanglement", "top_k": 3}
    search_res = client.post("/knowledge/search", json=search_payload)
    assert search_res.status_code == 200
    results = search_res.json()["results"]
    assert len(results) > 0
    assert results[0]["document_title"] == "Quantum Computing Principles"

    # Context Budget Allocation
    budget_payload = {
        "prompt": "How does quantum entanglement work?",
        "max_context_tokens": 2000,
        "document_ids": [doc_id],
    }
    budget_res = client.post("/knowledge/context-budget", json=budget_payload)
    assert budget_res.status_code == 200
    budget_data = budget_res.json()
    assert budget_data["max_tokens"] == 2000
    assert budget_data["used_tokens"] <= 2000
    assert len(budget_data["selected_chunks"]) > 0
