"""v5.1-v5.6 API: authentication, capabilities, flows, and unchanged defaults."""

import base64
import struct
import zlib
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.advanced_intelligence import IntelligenceLayer
from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TOKEN = "intelligence-test-token-with-at-least-32-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
USER = {"source_kind": "user_statement", "source_ref": "chat:1", "extractor": "user"}


def make_client(**overrides):
    values = dict(_env_file=None, environment="test", log_level="ERROR", local_auth_token=SecretStr(TOKEN), owner_id=OWNER)
    values.update(overrides)
    app = create_app(settings=Settings(**values))
    return app, TestClient(app, headers=HEADERS)


class DenyPolicy(CapabilityPolicy):
    def __init__(self, denied):
        super().__init__(OWNER)
        self.denied = denied

    def allows(self, owner_id, capability, **kwargs):
        return capability not in self.denied and super().allows(owner_id, capability, **kwargs)


ROUTES = [
    ("get", "/intelligence/models", None), ("post", "/intelligence/route", {"description": "debug code"}),
    ("get", "/intelligence/routing/recent", None),
    ("post", "/intelligence/evaluations", {"model_keys": ["mock-deterministic"]}),
    ("post", "/intelligence/graph/entities", {"entity_type": "service", "name": "api", "provenance": USER}),
    ("get", "/intelligence/graph/entities", None), ("post", "/intelligence/context/preview", {"query": "x"}),
    ("get", "/intelligence/memory/proposals", None),
    ("post", "/intelligence/multimodal/inspect", {"prompt": "x"}),
    ("post", "/intelligence/plans/evaluate", {"goal": "g", "steps": [{"step_id": "a", "description": "d"}]}),
    ("get", "/intelligence/reasoning/strategies", None), ("get", "/intelligence/preferences", None),
    ("put", "/intelligence/preferences/output_format", {"value": "table"}), ("post", "/intelligence/preferences/reset", None),
]


@pytest.mark.parametrize("method, path, body", ROUTES)
def test_every_route_requires_authentication(method, path, body):
    app, _ = make_client()
    with TestClient(app) as anonymous:
        assert anonymous.request(method, path, json=body).status_code == 401


@pytest.mark.parametrize("capability, method, path, body", [
    (Capability.INTELLIGENCE_READ, "get", "/intelligence/models", None),
    (Capability.INTELLIGENCE_READ, "post", "/intelligence/plans/evaluate",
     {"goal": "g", "steps": [{"step_id": "a", "description": "d"}]}),
    (Capability.INTELLIGENCE_EVALUATE, "post", "/intelligence/evaluations", {"model_keys": ["mock-deterministic"]}),
    (Capability.KNOWLEDGE_GRAPH_WRITE, "post", "/intelligence/graph/entities",
     {"entity_type": "service", "name": "api", "provenance": USER}),
    (Capability.PERSONALIZATION_MANAGE, "put", "/intelligence/preferences/output_format", {"value": "table"}),
    (Capability.PERSONALIZATION_MANAGE, "post", "/intelligence/preferences/reset", None),
    (Capability.MEMORY_WRITE, "post", "/intelligence/memory/proposals/outdated", {"target_ref": "memory:x", "reason": "r"}),
    (Capability.MEMORY_READ, "post", "/intelligence/context/preview", {"query": "x"}),
])
def test_capabilities_are_enforced_server_side(capability, method, path, body):
    app, client = make_client()
    with client:
        app.state.authorization_policy = DenyPolicy({capability})
        assert client.request(method, path, json=body).status_code == 403


def test_models_and_routing_in_default_mock_mode():
    app, client = make_client()
    with client:
        listing = client.get("/intelligence/models").json()
        status = {item["model"]["model_key"]: item for item in listing["models"]}
        assert status["mock-deterministic"]["available"] is True
        assert status["openai-compatible-configured"]["available"] is False
        assert "Capabilities are declared" in listing["note"] and listing["evidence"] == []
        routed = client.post("/intelligence/route", json={"description": "Debug this Python traceback"}).json()
        assert routed["decision"]["selected"] == "mock-deterministic" and routed["decision"]["demo"] is True
        assert routed["decision"]["requirements"]["task_type"] == "coding" and routed["signals"]
        assert client.post("/intelligence/route", json={}).status_code == 422
        assert client.post("/intelligence/route", json={"description": "x", "policy": "nope"}).status_code == 422
        assert client.post("/intelligence/route", json={"description": "x", "execute": True}).status_code == 422


def test_mock_evaluation_is_simulated_and_never_accepted_as_evidence():
    app, client = make_client()
    with client:
        report = client.post("/intelligence/evaluations", json={"model_keys": ["mock-deterministic"]}).json()
        assert report["summaries"][0]["simulated"] is True and report["summaries"][0]["correctness_rate"] == 0.0
        accepted = client.post(f"/intelligence/evaluations/{report['report_id']}/accept").json()
        assert accepted["accepted_models"] == []
        assert client.post("/intelligence/evaluations/ER-0000000000000000/accept").status_code == 404
        assert client.post("/intelligence/evaluations", json={"model_keys": ["unknown"]}).status_code == 422
        assert client.post("/intelligence/evaluations", json={"model_keys": ["a", "b", "c", "d"]}).status_code == 422


def test_graph_flow_over_the_api():
    app, client = make_client()
    with client:
        api = client.post("/intelligence/graph/entities", json={"entity_type": "service", "name": "API", "provenance": USER}).json()
        db = client.post("/intelligence/graph/entities", json={"entity_type": "service", "name": "DB", "provenance": USER}).json()
        edge = client.post("/intelligence/graph/relationships", json={
            "subject_id": api["entity_id"], "relation": "depends_on", "object_id": db["entity_id"], "status": "inferred",
            "confidence": 0.7, "provenance": {**USER, "extractor": "model", "source_kind": "document", "source_ref": "doc:1"}})
        assert edge.status_code == 200 and edge.json()["status"] == "inferred"
        view = client.get(f"/intelligence/graph/entities/{api['entity_id']}/neighbors", params={"depth": 2}).json()
        assert len(view["entities"]) == 2 and "not verified" in view["note"]
        assert client.get("/intelligence/graph/entities", params={"text": "db"}).json()["durable"] is False
        bad = client.post("/intelligence/graph/relationships", json={
            "subject_id": api["entity_id"], "relation": "describes", "object_id": db["entity_id"], "status": "verified",
            "confidence": 1.0, "provenance": USER})
        assert bad.status_code == 422
        assert client.get("/intelligence/graph/entities/KE-0000000000000000").status_code == 404
        assert client.delete(f"/intelligence/graph/entities/{api['entity_id']}").json()["removed"] == 2


def test_context_preview_requires_persistence_and_memory_proposals_are_owner_decisions():
    app, client = make_client()
    with client:
        assert client.post("/intelligence/context/preview", json={"query": "database"}).status_code == 503
        proposal = client.post("/intelligence/memory/proposals/outdated",
                               json={"target_ref": "memory:1", "reason": "Upgraded"}).json()
        assert proposal["status"] == "proposed"
        decided = client.post(f"/intelligence/memory/proposals/{proposal['proposal_id']}/accept").json()
        assert decided["status"] == "accepted"
        assert app.state.intelligence.memory_updates.outdated_refs(OWNER) == frozenset({"memory:1"})
        assert client.get("/intelligence/memory/proposals").json()["durable"] is False


def test_multimodal_inspect_reports_no_supporting_model_by_default():
    app, client = make_client()
    raw = b"".join(b"\x00\x00\x00\x00" for _ in range(1))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    with client:
        body = {"prompt": "what is this", "parts": [{"kind": "image", "media_type": "image/png", "source": "upload",
                                                     "data_base64": base64.b64encode(png).decode()}]}
        result = client.post("/intelligence/multimodal/inspect", json=body).json()
        assert result["eligible_models"] == [] and result["descriptors"][0]["width"] == 1
        assert "No configured model supports" in result["note"]
        body["parts"][0]["media_type"] = "image/gif"
        assert client.post("/intelligence/multimodal/inspect", json=body).status_code == 422


def test_reasoning_endpoints():
    app, client = make_client()
    with client:
        evaluation = client.post("/intelligence/plans/evaluate", json={"goal": "Deploy the service", "steps": [
            {"step_id": "deploy", "description": "Deploy the service", "action_kind": "consequential",
             "completion_criteria": "service reports healthy in production"}]}).json()
        assert evaluation["verdict"] == "reject" and "Nothing has been executed".lower() in evaluation["note"].lower()
        assert len(client.get("/intelligence/reasoning/strategies").json()["strategies"]) == 6
        check = client.post("/intelligence/answers/check", json={"task_class": "risk_analysis", "answer": "Fine."}).json()
        assert check["outcome"] == "disagree"
        assert client.post("/intelligence/answers/check", json={"task_class": "x", "answer": "y"}).status_code == 422


def test_personalization_flow_and_routing_effect():
    app, client = make_client()
    with client:
        assert client.put("/intelligence/preferences/output_format", json={"value": "table"}).status_code == 200
        assert client.put("/intelligence/preferences/allow_tools", json={"value": "yes"}).status_code == 422
        client.put("/intelligence/preferences/privacy_routing", json={"value": "local_only"})
        view = client.get("/intelligence/preferences").json()
        assert view["enabled"] is True and view["effective"]["values"]["output_format"] == "table"
        assert "formatting only" in view["presentation"]
        routed = client.post("/intelligence/route", json={"description": "summarize this"}).json()
        assert routed["personalized"] is True and routed["decision"]["requirements"]["privacy"] == "local_only"
        proposals = client.post("/intelligence/preferences/observations", json={"observations": [
            {"kind": "requested_shorter"}] * 3}).json()["proposals"]
        assert len(proposals) == 1
        decided = client.post(f"/intelligence/preferences/proposals/{proposals[0]['proposal_id']}/accept").json()
        assert decided["status"] == "accepted"
        assert client.post("/intelligence/preferences/disable").json() == {"enabled": False}
        assert client.get("/intelligence/preferences").json()["effective"]["values"] == {}
        assert client.post("/intelligence/preferences/reset").json()["removed"] >= 3
        assert client.get("/intelligence/preferences").json()["preferences"] == []


def test_defaults_and_execution_controls_are_unchanged():
    app, client = make_client()
    with client:
        assert isinstance(app.state.intelligence, IntelligenceLayer)
        assert app.state.settings.tools_enabled is False and app.state.settings.chat_tools_enabled is False
        assert app.state.settings.verification_execution_enabled is False
        assert app.state.orchestration_service.tool_registry.execution_enabled is False
        policy = app.state.authorization_policy
        assert policy.allows(OWNER, Capability.EXTERNAL_ACTION_EXECUTE) is False
        assert policy.allows(OWNER, Capability.VERIFY_EXECUTE) is False
        assert app.state.provider is app.state.intelligence.router.adapters["mock"] or app.state.provider.name == "mock"
        assert client.post("/chat", json={"message": "hello"}).status_code == 200
