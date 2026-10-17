"""v2.9 approval gateway and central audit trail tests.

Stores are exercised in memory and, for PostgreSQL, through a fake connection
that records SQL and parameters (no database is contacted). Approving a request
must never execute anything.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

import jarvis.audit.service as service_module
from database import runner
from jarvis.audit.models import ApprovalProposal, AuditRecord
from jarvis.audit.service import ApprovalError, ApprovalService, AuditService
from jarvis.audit.stores import (
    MemoryApprovalStore, MemoryAuditStore, PostgresApprovalStore, PostgresAuditStore, StorageError,
)
from jarvis.browser.providers import MockBrowserProvider, MockPage
from jarvis.browser.service import BrowserService
from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.orchestration.registry import ToolDefinition, ToolRegistry, ToolRegistryConfig
from jarvis.security.policy import Capability, CapabilityPolicy
from jarvis.tools.handlers import HandlerResult
from jarvis.tools.models import ToolStatus
from jarvis.tools.runtime import ToolRuntime

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TOKEN = "approvals-test-token-with-at-least-32-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def settings(**overrides):
    values = dict(_env_file=None, environment="test", log_level="ERROR", tools_enabled=True,
                  local_auth_token=SecretStr(TOKEN), owner_id=OWNER)
    values.update(overrides)
    return Settings(**values)


def proposal(**overrides):
    values = dict(category="tool", action="files.write", target="files.write", reason="Not low-risk.",
                  risk="high", expected_side_effects="Writes files.", scope_material='{"path":"a.txt"}',
                  correlation_id="req-1")
    values.update(overrides)
    return ApprovalProposal(**values)


def services(ttl=900, max_pending=100):
    audit = AuditService(MemoryAuditStore())
    return audit, ApprovalService(MemoryApprovalStore(), audit, ttl_seconds=ttl, max_pending=max_pending)


# ------------------------------------------------------------ contracts

def test_contracts_are_strict_and_execution_is_never_available():
    audit, approvals = services()
    item = approvals.request(OWNER, proposal())
    assert item.execution == "not_available" and item.state == "pending" and item.durable is False
    with pytest.raises(ValidationError):
        item.model_validate({**item.model_dump(), "execution": "executed"})
    with pytest.raises(ValidationError):
        item.model_validate({**item.model_dump(), "approval_id": "AR-nothex"})
    with pytest.raises(ValidationError):
        ApprovalProposal(**{**proposal().model_dump(), "category": "shell"})
    with pytest.raises(ValidationError):
        AuditRecord.model_validate({**audit.page(OWNER, limit=1).events[0].model_dump(), "extra": 1})
    assert "scope_material" not in item.model_dump()


# ------------------------------------------------------------- lifecycle

def test_approval_lifecycle_is_audited_and_executes_nothing():
    audit, approvals = services()
    item = approvals.request(OWNER, proposal())
    decided = approvals.decide(OWNER, item.approval_id, approve=True, note="ok")
    assert decided.state == "approved" and decided.decided_at and decided.decision_note == "ok"
    assert decided.execution == "not_available"
    events = audit.page(OWNER, limit=10).events
    assert [event.event_type for event in events] == ["approval_approved", "approval_requested"]
    assert events[0].details["executed"] is False


def test_a_request_is_decided_at_most_once():
    audit, approvals = services()
    item = approvals.request(OWNER, proposal())
    approvals.decide(OWNER, item.approval_id, approve=False, note=None)
    with pytest.raises(ApprovalError) as raised:
        approvals.decide(OWNER, item.approval_id, approve=True, note=None)
    assert raised.value.status_code == 409 and "rejected" in raised.value.public_message
    assert approvals.get(OWNER, item.approval_id).state == "rejected"
    assert audit.page(OWNER, limit=1).events[0].event_type == "approval_decision_refused"


def test_unknown_and_foreign_requests_are_not_found():
    _, approvals = services()
    item = approvals.request(OWNER, proposal())
    for owner, approval_id in ((OWNER, "AR-0000000000000000"), (OTHER, item.approval_id)):
        with pytest.raises(ApprovalError) as raised:
            approvals.decide(owner, approval_id, approve=True, note=None)
        assert raised.value.status_code == 404
    assert approvals.list(OTHER, limit=10).approvals == ()
    assert approvals.get(OWNER, item.approval_id).state == "pending"


def test_expired_requests_cannot_be_approved(monkeypatch):
    _, approvals = services(ttl=60)
    item = approvals.request(OWNER, proposal())
    later = datetime.now(timezone.utc) + timedelta(seconds=61)
    monkeypatch.setattr(service_module, "now_utc", lambda: later)
    with pytest.raises(ApprovalError) as raised:
        approvals.decide(OWNER, item.approval_id, approve=True, note=None)
    assert raised.value.status_code == 409
    assert approvals.get(OWNER, item.approval_id).state == "expired"


def test_memory_store_refuses_decisions_at_expiry_even_without_sweeping():
    store = MemoryApprovalStore()
    _, approvals = services()
    item = approvals.request(OWNER, proposal())
    store.create(item)
    at_expiry = datetime.fromisoformat(item.expires_at)
    assert store.decide(OWNER, item.approval_id, state="approved", note=None, now=at_expiry) is None


def test_identical_pending_requests_are_deduplicated_and_pending_is_bounded():
    audit, approvals = services(max_pending=2)
    first = approvals.request(OWNER, proposal())
    assert approvals.request(OWNER, proposal()).approval_id == first.approval_id
    assert approvals.request(OWNER, proposal(scope_material="other")).approval_id != first.approval_id
    assert approvals.request(OWNER, proposal(scope_material="third")) is None
    assert audit.page(OWNER, limit=1).events[0].status == "limit"
    # A different owner has its own budget.
    assert approvals.request(OTHER, proposal()) is not None


def test_approval_text_is_redacted_and_bounded():
    _, approvals = services()
    with pytest.raises(ValidationError):
        proposal(target="x" * 301)
    item = approvals.request(OWNER, proposal(target="key sk-ant-" + "a" * 40))
    assert "sk-ant-" + "a" * 40 not in item.target and len(item.target) <= 300


class FailingStore:
    durable = True

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise StorageError()
        return fail


def test_storage_failures_fail_safely():
    audit = AuditService(FailingStore())
    approvals = ApprovalService(FailingStore(), audit, ttl_seconds=900)
    assert approvals.request(OWNER, proposal()) is None
    for call in (lambda: approvals.list(OWNER, limit=5), lambda: approvals.get(OWNER, "AR-0000000000000000"),
                 lambda: approvals.decide(OWNER, "AR-0000000000000000", approve=True, note=None)):
        with pytest.raises(ApprovalError) as raised:
            call()
        assert raised.value.status_code == 503


def test_durable_audit_failure_falls_back_and_is_never_reported_durable():
    audit = AuditService(FailingStore())
    record = audit.record(owner_id=OWNER, category="tool", event_type="tool_call", decision="allowed",
                          status="succeeded", subject="calculator", details={"token": "sk-ant-" + "b" * 40})
    assert record.durable is False and audit.failures == 1
    assert "sk-ant-" not in str(record.details)
    page = audit.page(OWNER, limit=5)
    assert page.durable_storage is True and [event.event_id for event in page.events] == [record.event_id]
    assert "unavailable" in page.storage_note and "could not be stored durably" in page.storage_note


def test_audit_page_is_owner_scoped_filtered_and_bounded():
    audit = AuditService(MemoryAuditStore(capacity=5))
    for index in range(8):
        audit.record(owner_id=OWNER, category="tool" if index % 2 else "browser", event_type="e", decision="d",
                     status="s", subject=f"s{index}")
    audit.record(owner_id=OTHER, category="tool", event_type="e", decision="d", status="s", subject="other")
    page = audit.page(OWNER, limit=10)
    assert page.durable_storage is False and "In-memory" in page.storage_note
    assert all(event.owner_id == OWNER for event in page.events) and len(page.events) == 4
    assert {event.category for event in audit.page(OWNER, limit=10, category="tool").events} == {"tool"}


# --------------------------------------------------------------- postgres

class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=()):
        self.log.append((" ".join(query.split()), params))
        return FakeCursor(self.rows if "set_config" not in query else [])


def fake_connect(log, rows=(), fail=False):
    def connect(url, connect_timeout):
        log.append(("connect", connect_timeout))
        if fail:
            raise OSError("connection refused for postgresql://user:secret@host/db")
        return FakeConnection(log, list(rows))
    return connect


def approval_row(state="pending"):
    now = datetime.now(timezone.utc)
    return ("AR-" + "a" * 16, OWNER, "tool", "files.write", "files.write", "r", "high", "fx", "c" * 64, None,
            now, now + timedelta(minutes=15), state, now if state != "pending" else None, None)


def test_postgres_decision_is_one_conditional_owner_scoped_update():
    log = []
    store = PostgresApprovalStore("postgresql://x", connect=fake_connect(log, [approval_row("approved")]))
    now = datetime.now(timezone.utc)
    decided = store.decide(OWNER, "AR-" + "a" * 16, state="approved", note="n", now=now)
    assert decided.state == "approved" and decided.durable is True
    statements = [entry for entry in log if entry[0] != "connect" and "set_config" not in entry[0]]
    query, params = statements[0]
    assert len(statements) == 1 and query.startswith("UPDATE jarvis_app.approval_requests")
    assert "WHERE id = %s AND owner_id = %s AND state = 'pending' AND expires_at > %s" in query
    assert params == ("approved", now, "n", "AR-" + "a" * 16, OWNER, now)
    assert any("set_config('statement_timeout'" in entry[0] for entry in log)


def test_postgres_refused_decision_returns_none_and_queries_are_parameterized():
    log = []
    store = PostgresApprovalStore("postgresql://x", connect=fake_connect(log, []))
    assert store.decide(OWNER, "AR-" + "a" * 16, state="approved", note=None, now=datetime.now(timezone.utc)) is None
    assert store.get(OWNER, "AR-'; DROP TABLE x;--") is None
    assert all("DROP" not in entry[0] for entry in log if entry[0] != "connect")
    store.list(OWNER, limit=5, state="pending")
    assert log[-1][1] == (OWNER, "pending", 5)


def test_postgres_audit_store_round_trip_and_sanitized_failures():
    log = []
    _, approvals = services()
    audit = AuditService(PostgresAuditStore("postgresql://x", connect=fake_connect(log, [
        ("AE-" + "1" * 16, OWNER, datetime.now(timezone.utc), "tool", "tool_call", "allowed", "succeeded",
         "calculator", None, {"duration_ms": 3}),
    ])))
    record = audit.record(owner_id=OWNER, category="tool", event_type="tool_call", decision="allowed",
                          status="succeeded", subject="calculator")
    assert record.durable is True
    insert = next(entry for entry in log if entry[0].startswith("INSERT INTO jarvis_app.audit_events"))
    assert insert[1][0] == record.event_id and insert[1][1] == OWNER
    page = audit.page(OWNER, limit=5)
    assert page.durable_storage is True and page.events[0].details == {"duration_ms": 3}
    failing = PostgresAuditStore("postgresql://x", connect=fake_connect([], fail=True))
    with pytest.raises(StorageError) as raised:
        failing.append(record)
    assert "secret" not in str(raised.value)


def test_migration_0006_is_discovered_and_checksummed():
    found = {migration_id: checksum for migration_id, _, _, checksum in runner._migration_files(runner.MIGRATIONS_DIR)}
    assert "0006_audit_and_approvals" in found and len(found["0006_audit_and_approvals"]) == 64
    sql = (runner.MIGRATIONS_DIR / "0006_audit_and_approvals.sql").read_text(encoding="utf-8")
    assert "approval_requests_decision_consistent" in sql and "execut" not in sql.lower().replace(
        "no column or state represents execution", "")


# ---------------------------------------------------- runtime and browser

def risky_runtime():
    from tests.test_tools import handler, registry_for

    calls = []
    handlers = {"test.echo": handler(run=lambda ctx, args: calls.append(1) or HandlerResult(None))}
    tools = ToolRuntime(settings(), registry_for(handlers, risk="high"), CapabilityPolicy(OWNER), handlers=handlers)
    audit, approvals = services()
    tools.audit_service, tools.approval_service = audit, approvals
    return tools, audit, approvals, calls


def test_tool_requiring_approval_records_a_request_and_never_runs():
    from tests.test_tools import context

    tools, audit, approvals, calls = risky_runtime()
    result = tools.invoke(OWNER, "test.echo", {"text": "x"}, context())
    assert result.status == ToolStatus.APPROVAL_REQUIRED and calls == []
    pending = approvals.list(OWNER, limit=5).approvals
    assert len(pending) == 1 and result.evidence == (f"approval:{pending[0].approval_id}",)
    assert pending[0].risk == "high" and "x" not in pending[0].target
    approvals.decide(OWNER, pending[0].approval_id, approve=True, note=None)
    assert calls == []
    types = [event.event_type for event in audit.page(OWNER, limit=10).events]
    assert types.count("tool_call") == 1 and "approval_approved" in types


def test_runtime_approval_failure_still_refuses_execution():
    from tests.test_tools import context

    tools, _, _, calls = risky_runtime()
    tools.approval_service = ApprovalService(FailingStore(), AuditService(MemoryAuditStore()), ttl_seconds=900)
    result = tools.invoke(OWNER, "test.echo", {"text": "x"}, context())
    assert result.status == ToolStatus.APPROVAL_REQUIRED and result.evidence == () and calls == []


SITE = {"https://site.example/": MockPage(
    "<title>Home</title><form action='/pay'><input name='q' aria-label='Query'>"
    "<button type='submit'>Pay now</button></form>")}


def test_consequential_browser_click_records_an_approval_and_does_not_click():
    app = create_app(settings=settings(browser_enabled=True, browser_provider="mock"))
    app.state.browser_service = BrowserService(app.state.settings, MockBrowserProvider(SITE))
    app.state.browser_service.audit_service = app.state.audit_service
    app.state.browser_service.approval_service = app.state.approval_service
    with TestClient(app, headers=HEADERS) as client:
        session = client.post("/browser/sessions", json={"url": "https://site.example/"}).json()
        stored = app.state.browser_service._sessions[session["session_id"]]
        calls = []
        stored.context.click = lambda item: calls.append(item)
        observation = session["last_observation"]
        pay = next(item for item in observation["elements"] if item["label"] == "Pay now")
        result = client.post(f"/browser/sessions/{session['session_id']}/actions", json={
            "action": "click", "element_id": pay["element_id"], "observation_id": observation["observation_id"],
        }).json()
        assert result["record"]["outcome"] == "approval_required" and calls == []
        approvals = client.get("/approvals").json()
        item = approvals["approvals"][0]
        assert item["category"] == "browser" and item["risk"] == "consequential"
        assert item["approval_id"] in result["record"]["reason"]
        approved = client.post(f"/approvals/{item['approval_id']}/approve", json={}).json()
        assert approved["state"] == "approved" and approved["execution"] == "not_available" and calls == []
        categories = {event["category"] for event in client.get("/activity").json()["events"]}
        assert categories == {"browser", "approval"}


# ------------------------------------------------------------------ API

def make_client(**overrides):
    app = create_app(settings=settings(**overrides))
    return app, TestClient(app, headers=HEADERS)


def seed(app):
    return app.state.approval_service.request(OWNER, proposal())


@pytest.mark.parametrize("method, path", [
    ("get", "/activity"), ("get", "/approvals"), ("get", "/approvals/AR-0000000000000000"),
    ("post", "/approvals/AR-0000000000000000/approve"), ("post", "/approvals/AR-0000000000000000/reject"),
])
def test_routes_require_authentication(method, path):
    app, _ = make_client()
    with TestClient(app) as anonymous:
        response = anonymous.request(method, path, json={} if method == "post" else None)
        assert response.status_code == 401


class DenyPolicy(CapabilityPolicy):
    def __init__(self, denied):
        super().__init__(OWNER)
        self.denied = denied

    def allows(self, owner_id, capability, **kwargs):
        return capability not in self.denied and super().allows(owner_id, capability, **kwargs)


def test_routes_enforce_each_capability_server_side():
    app, client = make_client()
    with client:
        item = seed(app)
        app.state.authorization_policy = DenyPolicy({Capability.AUDIT_READ})
        assert client.get("/activity").status_code == 403
        app.state.authorization_policy = DenyPolicy({Capability.APPROVALS_READ})
        assert client.get("/approvals").status_code == 403
        assert client.post(f"/approvals/{item.approval_id}/approve", json={}).status_code == 403
        app.state.authorization_policy = DenyPolicy({Capability.APPROVALS_DECIDE})
        assert client.get("/approvals").status_code == 200
        assert client.post(f"/approvals/{item.approval_id}/approve", json={}).status_code == 403
        assert client.post(f"/approvals/{item.approval_id}/reject", json={}).status_code == 403
        assert app.state.approval_service.get(OWNER, item.approval_id).state == "pending"


def test_api_approval_flow_records_only():
    app, client = make_client()
    with client:
        item = seed(app)
        listing = client.get("/approvals", params={"state": "pending"}).json()
        assert listing["durable_storage"] is False and "nothing is executed" in listing["execution_note"]
        assert [entry["approval_id"] for entry in listing["approvals"]] == [item.approval_id]
        rejected = client.post(f"/approvals/{item.approval_id}/reject", json={"note": "no"}).json()
        assert rejected["state"] == "rejected" and rejected["execution"] == "not_available"
        again = client.post(f"/approvals/{item.approval_id}/approve", json={})
        assert again.status_code == 409
        assert client.get("/approvals/AR-0000000000000000").status_code == 404
        assert client.post(f"/approvals/{item.approval_id}/approve", json={"note": "x", "execute": True}).status_code == 422
        assert client.post(f"/approvals/{item.approval_id}/approve", json={"note": "x" * 1000}).status_code == 422
        assert client.get("/approvals", params={"state": "executed"}).status_code == 422
        activity = client.get("/activity", params={"category": "approval"}).json()
        assert activity["durable_storage"] is False and "In-memory" in activity["storage_note"]
        assert {event["event_type"] for event in activity["events"]} >= {"approval_requested", "approval_rejected"}
        assert client.get("/activity", params={"limit": 500}).status_code == 422


def test_tool_calls_appear_in_central_activity():
    app, client = make_client()
    with client:
        client.post("/tools/calls", json={"tool_id": "calculator", "arguments": {"expression": "6*7"}})
        events = client.get("/activity", params={"category": "tool"}).json()["events"]
        assert events[0]["subject"] == "calculator" and events[0]["status"] == "succeeded"
        assert "6*7" not in str(events[0])


def test_defaults_and_execution_controls_are_unchanged():
    app = create_app(settings=Settings(_env_file=None, environment="test", log_level="ERROR",
                                       local_auth_token=SecretStr(TOKEN), owner_id=OWNER))
    assert app.state.settings.tools_enabled is False and app.state.settings.approvals_ttl_seconds == 900
    assert app.state.settings.verification_execution_enabled is False
    assert app.state.orchestration_service.tool_registry.execution_enabled is False
    policy = app.state.authorization_policy
    assert policy.allows(OWNER, Capability.EXTERNAL_ACTION_EXECUTE) is False
    assert policy.allows(OWNER, Capability.APPROVALS_DECIDE) is True
    assert policy.allows(OTHER, Capability.APPROVALS_DECIDE) is False
    assert app.state.tool_runtime.approval_service is app.state.approval_service
    assert app.state.browser_service.audit_service is app.state.audit_service
    assert isinstance(app.state.audit_service.store, MemoryAuditStore)
