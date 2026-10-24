"""API integration tests for the v3.4 Research & Document Intelligence routes.

Tests /research/* endpoints with FastAPI TestClient:
- Authentication & Capability enforcement (401, 403)
- Multi-format document parsing (markdown, text, csv, json)
- Document listing, detail, deletion
- Cited research synthesis creation, listing, detail
- 404, 413, and 422 error handling
"""

from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy

TEST_TOKEN = "research-test-token-at-least-32-chars"
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
    res = no_auth_client.get("/research/documents")
    assert res.status_code == 401

    res = no_auth_client.post(
        "/research/documents/parse",
        json={"filename": "doc.txt", "content_type": "text/plain", "text_content": "plain text"},
    )
    assert res.status_code == 401


def test_capability_enforcement(client):
    client.app_ref.state.authorization_policy = DenyPolicy({Capability.RESEARCH_WRITE})

    # Read should succeed
    res = client.get("/research/documents")
    assert res.status_code == 200

    # Write should fail (403)
    res = client.post(
        "/research/documents/parse",
        json={"filename": "doc.txt", "content_type": "text/plain", "text_content": "plain text"},
    )
    assert res.status_code == 403


# ===================================================================
# 2. Document Parsing & CRUD
# ===================================================================

def test_document_parse_and_crud(client):
    # Parse markdown document
    payload = {
        "filename": "findings.md",
        "content_type": "text/markdown",
        "text_content": "# Research Findings\nKey insights on resilient systems.",
    }
    parse_res = client.post("/research/documents/parse", json=payload)
    assert parse_res.status_code == 201
    doc = parse_res.json()
    doc_id = doc["id"]
    assert doc["filename"] == "findings.md"
    assert doc["content_type"] == "text/markdown"
    assert "<<<UNTRUSTED_DOCUMENT_CONTENT" in doc["extracted_text"]

    # Detail
    get_res = client.get(f"/research/documents/{doc_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == doc_id

    # List
    list_res = client.get("/research/documents")
    assert list_res.status_code == 200
    assert list_res.json()["total"] == 1
    assert list_res.json()["items"][0]["id"] == doc_id

    # Delete
    del_res = client.delete(f"/research/documents/{doc_id}")
    assert del_res.status_code == 204

    # 404 after delete
    assert client.get(f"/research/documents/{doc_id}").status_code == 404
    assert client.delete(f"/research/documents/{doc_id}").status_code == 404


# ===================================================================
# 3. Research Synthesis API
# ===================================================================

def test_synthesis_api(client):
    # Parse 2 documents
    p1 = client.post(
        "/research/documents/parse",
        json={"filename": "doc1.txt", "content_type": "text/plain", "text_content": "Primary research data."},
    ).json()
    p2 = client.post(
        "/research/documents/parse",
        json={"filename": "doc2.txt", "content_type": "text/plain", "text_content": "Secondary analysis findings."},
    ).json()

    # Synthesize
    synth_req = {
        "topic": "system architecture",
        "document_ids": [p1["id"], p2["id"]],
        "web_urls": ["https://example.com/spec"],
    }
    res = client.post("/research/synthesize", json=synth_req)
    assert res.status_code == 201
    synth = res.json()
    synth_id = synth["id"]
    assert synth["topic"] == "system architecture"
    assert len(synth["citations"]) >= 2

    # Get synthesis
    get_res = client.get(f"/research/syntheses/{synth_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == synth_id

    # List syntheses
    list_res = client.get("/research/syntheses")
    assert list_res.status_code == 200
    assert list_res.json()["total"] == 1

    # 404 on missing
    missing_id = uuid4()
    assert client.get(f"/research/syntheses/{missing_id}").status_code == 404


# ===================================================================
# 4. Error Handling (413, 422)
# ===================================================================

def test_oversized_document_rejected(client):
    oversized = "A" * (10 * 1024 * 1024 + 100)
    res = client.post(
        "/research/documents/parse",
        json={"filename": "huge.txt", "content_type": "text/plain", "text_content": oversized},
    )
    assert res.status_code == 413


def test_invalid_json_parsing_error(client):
    res = client.post(
        "/research/documents/parse",
        json={"filename": "broken.json", "content_type": "application/json", "text_content": "not valid json"},
    )
    assert res.status_code == 422
