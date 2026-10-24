"""v4.2 correlation, audit consistency and truthful status."""

import json
import logging
import threading
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import jarvis.health.probes as probes
from database import runner
from jarvis.core.config import Settings
from jarvis.core.correlation import _current, accept_or_generate, get_correlation_id
from jarvis.core.failure_audit import category_for, template_path
from jarvis.core.logging_config import JsonFormatter
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TOKEN = "v42-observability-token-with-at-least-32-chars"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def make_client(**overrides):
    values = dict(_env_file=None, environment="test", log_level="ERROR", local_auth_token=SecretStr(TOKEN), owner_id=OWNER)
    values.update(overrides)
    app = create_app(settings=Settings(**values))
    return app, TestClient(app, headers=HEADERS)


def events(client, **params):
    return client.get("/activity", params={"limit": 200, **params}).json()["events"]


class DenyPolicy(CapabilityPolicy):
    def __init__(self, denied):
        super().__init__(OWNER)
        self.denied = denied

    def allows(self, owner_id, capability, **kwargs):
        return capability not in self.denied and super().allows(owner_id, capability, **kwargs)


# ------------------------------------------------------------- correlation

def test_every_response_carries_a_safe_correlation_id():
    app, client = make_client()
    with client:
        generated = client.get("/health").headers["x-request-id"]
        assert generated.startswith("req-") and len(generated) == 20
        assert client.get("/health", headers={"X-Request-ID": "client.trace-42"}).headers["x-request-id"] == "client.trace-42"
        for hostile in ("bad id with spaces", "x" * 65, "line\nbreak", "<script>"):
            echoed = client.get("/health", headers={"X-Request-ID": hostile}).headers["x-request-id"]
            assert echoed != hostile and echoed.startswith("req-")
        assert len({client.get("/health").headers["x-request-id"] for _ in range(5)}) == 5
    assert get_correlation_id() is None  # the context is reset after each request


def test_correlation_id_reaches_audit_records_of_v3_services():
    app, client = make_client()
    with client:
        response = client.post("/assistant/tasks", json={"title": "Correlated task"}, headers={"X-Request-ID": "trace-v42-a"})
        assert response.status_code == 201 and response.headers["x-request-id"] == "trace-v42-a"
        record = next(item for item in events(client, category="assistant") if item["event_type"] == "task.create")
    assert record["correlation_id"] == "trace-v42-a"


def test_correlation_ids_are_isolated_between_concurrent_requests():
    seen, barrier = {}, threading.Barrier(4)

    def worker(index):
        token = _current.set(f"req-{index:016x}")
        try:
            barrier.wait(timeout=5)
            seen[index] = get_correlation_id()
        finally:
            _current.reset(token)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert seen == {i: f"req-{i:016x}" for i in range(4)}


def test_json_logs_include_the_correlation_id():
    record = logging.LogRecord("jarvis.test", logging.INFO, __file__, 1, "hello", None, None)
    token = _current.set("req-0123456789abcdef")
    try:
        payload = json.loads(JsonFormatter().format(record))
    finally:
        _current.reset(token)
    assert payload["correlation_id"] == "req-0123456789abcdef"
    assert "correlation_id" not in json.loads(JsonFormatter().format(record))
    assert accept_or_generate(None).startswith("req-")


# ------------------------------------------------------- audit consistency

def test_each_v3_subsystem_audits_under_its_own_category_with_standard_vocabulary():
    app, client = make_client()
    with client:
        client.post("/assistant/tasks", json={"title": "t"})
        client.post("/knowledge/documents", json={"title": "k", "content": "knowledge body text"})
        client.post("/research/documents/parse", json={"filename": "r.txt", "content_type": "text/plain", "text_content": "r"})
        client.post("/finance/accounts", json={"name": "a", "account_type": "checking", "institution": "x", "balance": 1.0})
        client.post("/scheduler/jobs", json={"name": "j", "schedule_type": "interval", "schedule_expr": "60", "action_type": "reminder"})
        client.post("/workflows/definitions", json={"name": "w", "dag_spec": {"steps": [{"step_id": "a", "name": "A"}]}})
        client.post("/health/diagnostic/snapshots")
        client.post("/improvement/proposals", json={"target_name": "prompts/summarizer.md", "proposed_content": "Be brief.",
                                                     "rationale": "shorter"})
        recorded = events(client)
    categories = {item["category"] for item in recorded}
    assert {"assistant", "knowledge", "research", "finance", "scheduler", "workflow", "health", "improvement"} <= categories
    assert "tool" not in categories
    for item in recorded:
        assert item["decision"] in {"allowed", "denied", "approval_required"} and item["status"] != "success"


def test_failed_and_denied_mutations_are_audited_without_request_content():
    app, client = make_client()
    with client:
        invalid = client.post("/assistant/tasks", json={"title": ""}, headers={"X-Request-ID": "trace-fail"})
        missing = client.delete("/finance/accounts/00000000-0000-4000-8000-000000000000")
        app.state.authorization_policy = DenyPolicy({Capability.KNOWLEDGE_WRITE})
        denied = client.post("/knowledge/documents", json={"title": "secret title", "content": "secret body"})
        app.state.authorization_policy = CapabilityPolicy(OWNER)
        client.get("/assistant/tasks/00000000-0000-4000-8000-000000000000")  # reads are not audited here
        recorded = [item for item in events(client) if item["event_type"] in {"request_failed", "request_denied"}]
    assert (invalid.status_code, missing.status_code, denied.status_code) == (422, 404, 403)
    by_category = {item["category"]: item for item in recorded}
    assert by_category["assistant"]["correlation_id"] == "trace-fail" and by_category["assistant"]["details"]["http_status"] == 422
    assert by_category["finance"]["subject"] == "DELETE /finance/accounts/{id}"
    assert by_category["knowledge"]["decision"] == "denied" and by_category["knowledge"]["event_type"] == "request_denied"
    assert all(item["status"] == "failed" and item["details"]["mutated"] is False for item in recorded)
    assert len(recorded) == 3 and "secret" not in json.dumps(recorded)


def test_unauthenticated_and_self_auditing_routes_are_not_double_counted():
    app, client = make_client()
    with TestClient(app) as anonymous:
        assert anonymous.post("/assistant/tasks", json={"title": "x"}).status_code == 401
    with client:
        assert client.post("/intelligence/graph/relationships", json={}).status_code == 422
        recorded = events(client)
    assert not any(item["event_type"] in {"request_failed", "request_denied"} for item in recorded)


def test_path_templating_and_category_mapping():
    assert template_path("/finance/accounts/0f8fad5b-d9cb-469f-a165-70867728950e/x") == "/finance/accounts/{id}/x"
    assert template_path("/intelligence/graph/entities/KE-0123456789abcdef") == "/intelligence/graph/entities/{id}"
    assert category_for("/health/security/quarantine") == "health" and category_for("/health") is None
    assert category_for("/chat") is None and category_for("/autonomous/execute") == "orchestration"


def test_migration_0060_keeps_every_category_and_matches_the_model():
    from typing import get_args

    from jarvis.audit.models import AuditCategory

    sql = (runner.MIGRATIONS_DIR / "0060_audit_subsystem_categories.sql").read_text(encoding="utf-8")
    for category in get_args(AuditCategory):
        assert f"'{category}'" in sql
    assert "DROP NOT NULL" in (runner.MIGRATIONS_DIR / "0061_health_telemetry_nullable.sql").read_text(encoding="utf-8")


# ------------------------------------------------------------ truthful status

def test_status_requires_authentication_and_capability():
    app, client = make_client()
    with TestClient(app) as anonymous:
        assert anonymous.get("/status").status_code == 401
    with client:
        app.state.authorization_policy = DenyPolicy({Capability.STATUS_READ})
        assert client.get("/status").status_code == 403


def test_status_reports_real_states_and_limitations():
    app, client = make_client()
    with client:
        report = client.get("/status", headers={"X-Request-ID": "trace-status"}).json()
    states = {item["key"]: item for item in report["subsystems"]}
    assert report["correlation_id"] == "trace-status" and report["overall"] == "operational"
    assert states["provider"]["state"] == "mock" and states["database"]["state"] == "disabled"
    assert states["scheduler"]["state"] == "limited" and any("manually" in text for text in states["scheduler"]["limitations"])
    assert states["verification_execution"]["state"] == "disabled" and states["multimodal"]["state"] == "disabled"
    assert states["assistant"]["storage"] == "memory" and states["model_routing"]["state"] == "mock"
    assert sum(report["counts"].values()) == len(report["subsystems"])


def test_unreachable_database_is_unavailable_and_never_leaks_the_url():
    secret_url = "postgresql://jarvis_user:" + "not-a-real-password@127.0.0.1:1/jarvis_missing"
    app, client = make_client(persistence_enabled=True, persistence_database_url=SecretStr(secret_url))
    with client:
        response = client.get("/status")
    report = response.json()
    database = next(item for item in report["subsystems"] if item["key"] == "database")
    assert database["state"] == "unavailable" and report["overall"] == "degraded"
    assert "not-a-real-password" not in response.text and "jarvis_user" not in response.text


# ------------------------------------------------------------- telemetry

def test_telemetry_is_measured_or_reported_unavailable(monkeypatch):
    values = probes.get_system_telemetry()
    assert values["cpu_percent"] is None or 0 <= values["cpu_percent"] <= 100
    assert values["memory_percent"] is None or 0 <= values["memory_percent"] <= 100
    assert (values["cpu_percent"], values["memory_percent"]) != (15.0, 42.0)
    monkeypatch.setattr(probes.sys, "platform", "plan9")
    assert probes.measure_cpu_percent() is None and probes.measure_memory_percent() is None

    def broken(path):
        raise OSError("no disk")

    monkeypatch.setattr(probes.shutil, "disk_usage", broken)
    assert probes.get_system_telemetry()["disk_free_gb"] is None


def test_health_snapshot_records_real_status_instead_of_fixed_labels():
    app, client = make_client()
    with client:
        snapshot = client.post("/health/diagnostic/snapshots").json()
        diagnostic = client.post("/health/diagnostic/run").json()
    assert snapshot["services_status"]["database"] == "disabled" and "api_server" not in snapshot["services_status"]
    assert diagnostic["recommendations"]


def test_scheduled_health_and_workflow_jobs_report_honestly():
    app, client = make_client()
    with client:
        health = client.post("/scheduler/jobs", json={"name": "h", "schedule_type": "interval", "schedule_expr": "60",
                                                      "action_type": "health_check"}).json()
        result = client.post(f"/scheduler/jobs/{health['id']}/run").json()
        workflow = client.post("/scheduler/jobs", json={"name": "w", "schedule_type": "interval", "schedule_expr": "60",
                                                        "action_type": "workflow", "payload": {"workflow_id": "x"}}).json()
        failed = client.post(f"/scheduler/jobs/{workflow['id']}/run").json()
    assert result["status"] == "success" and result["output"]["all_healthy"] is True
    assert result["output"]["checked_subsystems"] > 10
    assert failed["status"] == "failure" and "not connected" in failed["error"]


def test_autonomous_overview_has_no_fixed_marketing_claims():
    app, client = make_client()
    with client:
        overview = client.get("/autonomous/overview").json()
    text = json.dumps(overview)
    for claim in ("Hybrid BM25", "Vector RRF", "operational.", "Zero-trading AST invariant"):
        assert claim not in text
    assert overview["subsystems"]["scheduler"]["metrics"]["state"] == "limited"
    assert "manually" in overview["subsystems"]["scheduler"]["details"]


def test_execution_defaults_are_unchanged():
    app, client = make_client()
    assert app.state.settings.verification_execution_enabled is False
    assert app.state.settings.tools_enabled is False
    assert app.state.authorization_policy.allows(OWNER, Capability.EXTERNAL_ACTION_EXECUTE) is False
    assert app.state.authorization_policy.allows(OWNER, Capability.STATUS_READ) is True
