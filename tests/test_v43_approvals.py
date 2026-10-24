"""v4.3 enforced approvals: autonomous writes, improvement apply/restore and quarantine/restore."""

import re
import threading
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import jarvis.audit.service as approval_module
from approval_flow import approve_over_http, memory_approvals
from jarvis.audit.models import ENFORCED_CATEGORIES, ApprovalProposal, ApprovalCategory
from jarvis.audit.service import ApprovalError, ApprovalRequired, run_approved
from jarvis.core.config import Settings
from jarvis.health.quarantine import QuarantineManager
from jarvis.main import create_app

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TOKEN = "v43-enforced-approvals-token-with-32-chars"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
FINANCE = {"prompt": "Review my financial runway", "dry_run": False}
MIGRATION = Path(__file__).resolve().parents[1] / "database" / "scripts" / "0062_enforced_approvals.sql"
LATEST_CATEGORY_MIGRATION = MIGRATION.with_name("0065_integrations.sql")  # v4.8: the newest category list


def make_client(**overrides):
    values = dict(_env_file=None, environment="test", log_level="ERROR", local_auth_token=SecretStr(TOKEN), owner_id=OWNER)
    values.update(overrides)
    app = create_app(settings=Settings(**values))
    return app, TestClient(app, headers=HEADERS)


def execute(client, body, approval_id=None):
    return client.post("/autonomous/execute", json={**body, **({"approval_id": approval_id} if approval_id else {})})


def notes(client):
    return client.get("/assistant/notes").json()["total"]


def events(client, category="approval"):
    return client.get("/activity", params={"limit": 200, "category": category}).json()["events"]


def proposal(**overrides):
    values = dict(category="quarantine", action="security.quarantine", target="a.txt", reason="r", risk="medium",
                  expected_side_effects="moves a.txt", scope_material="a.txt|hash")
    values.update(overrides)
    return ApprovalProposal(**values)


@pytest.fixture
def quarantine_client(tmp_path):
    app, client = make_client()
    with client:
        app.state.health_service.quarantine_manager = QuarantineManager(tmp_path / ".quarantine", allowed_root=tmp_path)
        yield app, client, tmp_path


# ------------------------------------------------------------- autonomous

def test_autonomous_write_command_waits_for_approval_and_runs_nothing():
    app, client = make_client()
    with client:
        before = notes(client)
        waiting = execute(client, FINANCE)
        body = waiting.json()
        assert waiting.status_code == 202 and body["status"] == "approval_required"
        approval = body["approval"]
        assert (approval["category"], approval["action"], approval["state"], approval["execution"]) == (
            "autonomous", "autonomous.execute", "pending", "single_use")
        assert "assistant.notes.create" in approval["target"] and approval["approval_id"] in body["detail"]
        assert notes(client) == before and client.get("/autonomous/history").json() == []
        # Asking again for the same command returns the same pending request instead of a new one.
        assert execute(client, FINANCE).json()["approval"]["approval_id"] == approval["approval_id"]


def test_approved_autonomous_command_runs_exactly_once():
    app, client = make_client()
    with client:
        before = notes(client)
        result = approve_over_http(client, lambda approval_id: execute(client, FINANCE, approval_id))
        assert result.status_code == 200 and result.json()["status"] == "completed"
        approval_id = result.json()["approval_id"]
        assert notes(client) == before + 1
        reused = execute(client, FINANCE, approval_id)
        assert reused.status_code == 409 and "already been used" in reused.json()["detail"]
        assert notes(client) == before + 1
        approval = client.get(f"/approvals/{approval_id}").json()
        assert approval["state"] == "approved" and approval["consumed_at"] is not None
        types = [item["event_type"] for item in events(client)]
        assert {"approval_requested", "approval_approved", "approval_used", "approved_action_succeeded",
                "approval_use_refused"} <= set(types)


def test_approval_is_bound_to_the_exact_command():
    app, client = make_client()
    with client:
        approval_id = execute(client, FINANCE).json()["approval"]["approval_id"]
        client.post(f"/approvals/{approval_id}/approve", json={})
        other = execute(client, {**FINANCE, "prompt": "Review my financial runway and budget"}, approval_id)
        more_steps = execute(client, {**FINANCE, "max_steps": 2}, approval_id)
        assert other.status_code == 409 and more_steps.status_code == 409
        assert "different action or target" in other.json()["detail"]
        assert execute(client, FINANCE, approval_id).status_code == 200  # still usable for the approved command


def test_pending_rejected_unknown_and_foreign_approvals_run_nothing():
    app, client = make_client()
    with client:
        before = notes(client)
        approval_id = execute(client, FINANCE).json()["approval"]["approval_id"]
        pending = execute(client, FINANCE, approval_id)
        assert pending.status_code == 409 and "waiting for your decision" in pending.json()["detail"]
        client.post(f"/approvals/{approval_id}/reject", json={})
        rejected = execute(client, FINANCE, approval_id)
        assert rejected.status_code == 409 and "rejected" in rejected.json()["detail"]
        assert execute(client, FINANCE, "AR-" + "0" * 16).status_code == 404
        assert execute(client, FINANCE, "not-an-approval-id").status_code == 422
        assert notes(client) == before


def test_read_only_and_dry_run_commands_need_no_approval():
    app, client = make_client()
    with client:
        read_only = execute(client, {"prompt": "What do I know about gardening?", "max_steps": 1})
        dry_run = execute(client, {**FINANCE, "dry_run": True})
    assert read_only.status_code == 200 and read_only.json()["steps"][0]["action"] == "search"
    assert read_only.json()["approval_id"] is None
    assert dry_run.status_code == 200 and dry_run.json()["status"] == "dry_run"


# ------------------------------------------------------------- quarantine

def test_quarantine_waits_for_approval_and_is_bound_to_file_content(quarantine_client):
    app, client, root = quarantine_client
    suspect = root / "suspect.py"
    suspect.write_text("eval(input())", encoding="utf-8")
    body = {"file_path": "suspect.py", "reason": "eval on input"}
    waiting = client.post("/health/security/quarantine", json=body)
    assert waiting.status_code == 202 and suspect.exists()
    approval = waiting.json()["approval"]
    assert (approval["category"], approval["action"], approval["target"]) == ("quarantine", "security.quarantine",
                                                                              "suspect.py")
    client.post(f"/approvals/{approval['approval_id']}/approve", json={})
    suspect.write_text("eval(input())  # changed after approval", encoding="utf-8")
    changed = client.post("/health/security/quarantine", json=body, params={"approval_id": approval["approval_id"]})
    assert changed.status_code == 409 and suspect.exists()
    other_reason = client.post("/health/security/quarantine", json={**body, "reason": "something else"},
                               params={"approval_id": approval["approval_id"]})
    assert other_reason.status_code == 409


def test_quarantine_and_restore_each_need_their_own_approval(quarantine_client):
    app, client, root = quarantine_client
    suspect = root / "suspect.py"
    suspect.write_text("eval(input())", encoding="utf-8")
    body = {"file_path": "suspect.py", "reason": "eval on input"}
    moved = approve_over_http(client, lambda approval_id: client.post(
        "/health/security/quarantine", json=body, params={"approval_id": approval_id} if approval_id else {}))
    assert moved.status_code == 201 and not suspect.exists()
    record_id = moved.json()["id"]
    quarantine_approval = re.search(r"AR-[a-f0-9]{16}", str(events(client)[0]["details"])).group(0)
    wrong = client.post(f"/health/security/quarantined/{record_id}/restore", params={"approval_id": quarantine_approval})
    assert wrong.status_code == 409 and not suspect.exists()
    waiting = client.post(f"/health/security/quarantined/{record_id}/restore")
    assert waiting.status_code == 202 and waiting.json()["approval"]["action"] == "security.restore"
    assert not suspect.exists()
    restored = approve_over_http(client, lambda approval_id: client.post(
        f"/health/security/quarantined/{record_id}/restore", params={"approval_id": approval_id} if approval_id else {}))
    assert restored.status_code == 200 and suspect.exists()
    assert client.post(f"/health/security/quarantined/{record_id}/restore").status_code == 404  # already restored


def test_invalid_quarantine_targets_are_refused_before_any_approval_is_recorded(quarantine_client):
    app, client, root = quarantine_client
    assert client.post("/health/security/quarantine", json={"file_path": "missing.py", "reason": "x"}).status_code == 404
    assert client.post("/health/security/quarantine", json={"file_path": "../outside.py", "reason": "x"}).status_code == 422
    assert client.get("/approvals").json()["approvals"] == []


# ------------------------------------------------------------- improvement

def test_improvement_apply_and_restore_wait_for_approval():
    app, client = make_client()
    with client:
        service = app.state.improvement_service
        target = "prompts/summarizer.md"
        original = service.get_target_content(target)
        created = client.post("/improvement/proposals", json={
            "target_name": target, "proposed_content": "Summarize in one sentence.", "rationale": "Shorter"}).json()
        decide = f"/improvement/proposals/{created['id']}/decide"
        waiting = client.post(decide, json={"decision": "approved"})
        assert waiting.status_code == 202 and waiting.json()["approval"]["action"] == "improvement.apply"
        assert service.get_target_content(target) == original
        assert client.get(f"/improvement/proposals/{created['id']}").json()["status"] == "pending"
        applied = approve_over_http(client, lambda approval_id: client.post(
            decide, json={"decision": "approved"}, params={"approval_id": approval_id} if approval_id else {}))
        assert applied.status_code == 200 and service.get_target_content(target) == "Summarize in one sentence."
        restore = f"/improvement/proposals/{created['id']}/restore"
        assert client.post(restore).status_code == 202
        assert service.get_target_content(target) == "Summarize in one sentence."
        restored = approve_over_http(client, lambda approval_id: client.post(
            restore, params={"approval_id": approval_id} if approval_id else {}))
        assert restored.status_code == 200 and service.get_target_content(target) == original


def test_improvement_rejection_needs_no_approval_and_stale_targets_are_refused():
    app, client = make_client()
    with client:
        service = app.state.improvement_service
        target = "prompts/summarizer.md"
        first = client.post("/improvement/proposals", json={
            "target_name": target, "proposed_content": "One.", "rationale": "a"}).json()
        second = client.post("/improvement/proposals", json={
            "target_name": target, "proposed_content": "Two.", "rationale": "b"}).json()
        rejected = client.post(f"/improvement/proposals/{first['id']}/decide", json={"decision": "rejected"})
        assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
        service.set_target_content(target, "Changed by someone else.")
        stale = client.post(f"/improvement/proposals/{second['id']}/decide", json={"decision": "approved"})
        assert stale.status_code == 422 and "changed since" in stale.json()["detail"]
        assert client.get("/approvals").json()["approvals"] == []


# ------------------------------------------------------------- gateway rules

def test_missing_or_failing_gateway_runs_nothing():
    ran = []
    with pytest.raises(ApprovalError) as missing:
        run_approved(None, OWNER, proposal(), None, lambda: ran.append(1))
    approvals = memory_approvals()
    approvals.max_pending = 0
    with pytest.raises(ApprovalError) as full:
        run_approved(approvals, OWNER, proposal(), None, lambda: ran.append(1))
    assert (missing.value.status_code, full.value.status_code, ran) == (503, 503, [])


def test_approval_window_ends_after_the_decision(monkeypatch):
    approvals = memory_approvals(ttl_seconds=60)
    with pytest.raises(ApprovalRequired) as required:
        run_approved(approvals, OWNER, proposal(), None, lambda: "ran")
    approval_id = required.value.request.approval_id
    approvals.decide(OWNER, approval_id, approve=True, note=None)
    later = approval_module.now_utc() + timedelta(seconds=61)
    monkeypatch.setattr(approval_module, "now_utc", lambda: later)
    with pytest.raises(ApprovalError) as ended:
        run_approved(approvals, OWNER, proposal(), approval_id, lambda: "ran")
    assert ended.value.status_code == 409 and "window has ended" in ended.value.public_message


def test_concurrent_use_of_one_approval_runs_the_action_once():
    approvals = memory_approvals()
    with pytest.raises(ApprovalRequired) as required:
        run_approved(approvals, OWNER, proposal(), None, lambda: None)
    approval_id = required.value.request.approval_id
    approvals.decide(OWNER, approval_id, approve=True, note=None)
    ran, refused, barrier = [], [], threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            run_approved(approvals, OWNER, proposal(), approval_id, lambda: ran.append(1))
        except ApprovalError as exc:
            refused.append(exc.status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert ran == [1] and refused == [409] * 7


def test_failed_approved_action_is_audited_and_the_approval_stays_used():
    approvals = memory_approvals()
    with pytest.raises(ApprovalRequired) as required:
        run_approved(approvals, OWNER, proposal(), None, lambda: None)
    approval_id = required.value.request.approval_id
    approvals.decide(OWNER, approval_id, approve=True, note=None)

    def boom():
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        run_approved(approvals, OWNER, proposal(), approval_id, boom)
    page = approvals.audit.page(OWNER, limit=20)
    failed = next(item for item in page.events if item.event_type == "approved_action_failed")
    assert failed.details["error_type"] == "RuntimeError" and "disk full" not in str(failed.details)
    with pytest.raises(ApprovalError):
        run_approved(approvals, OWNER, proposal(), approval_id, lambda: None)


def test_tool_and_browser_approvals_stay_record_only():
    approvals = memory_approvals()
    tool = approvals.request(OWNER, proposal(category="tool", action="files.write"))
    gated = approvals.request(OWNER, proposal())
    assert (tool.execution, gated.execution) == ("not_available", "single_use")
    approvals.decide(OWNER, tool.approval_id, approve=True, note=None)
    with pytest.raises(ApprovalError):  # a tool approval cannot be used to run a gated action
        run_approved(approvals, OWNER, proposal(), tool.approval_id, lambda: None)


def test_migration_0062_matches_the_model():
    def categories(path):
        sql = path.read_text(encoding="utf-8")
        found = re.search(r"approval_requests_category_allowed\s+CHECK \(category IN \(([^)]*)\)", sql)
        return set(re.sub(r"[\s']", "", found.group(1)).split(","))

    sql = MIGRATION.read_text(encoding="utf-8")
    allowed = categories(LATEST_CATEGORY_MIGRATION)  # later migrations keep every 0062 category and add more
    assert categories(MIGRATION) < allowed
    assert allowed == set(ApprovalCategory.__args__) and ENFORCED_CATEGORIES < allowed
    assert "ADD COLUMN consumed_at timestamptz" in sql and "DROP TABLE" not in sql.upper()


def test_status_and_execution_note_describe_enforcement():
    app, client = make_client()
    with client:
        report = client.get("/status").json()
        note = client.get("/approvals").json()["execution_note"]
    text = str(report)
    assert "self-approval" not in text and "Writes without approval" not in text
    assert "run once" in note.replace("runs once", "run once")


# ------------------------------------------------------------- review fixes

def test_longest_allowed_prompt_is_gated_not_crashed():
    app, client = make_client()
    with client:
        body = {"prompt": "finance " + "x" * 992, "dry_run": False}
        result = approve_over_http(client, lambda approval_id: execute(client, body, approval_id))
    assert result.status_code == 200 and result.json()["status"] == "completed"


def test_failed_approved_autonomous_run_is_audited_as_failed(monkeypatch):
    app, client = make_client()
    with client:
        def broken(owner_id):
            raise RuntimeError("snapshot store down")

        monkeypatch.setattr(app.state.finance_service, "generate_snapshot", broken)
        result = approve_over_http(client, lambda approval_id: execute(client, FINANCE, approval_id))
        types = [item["event_type"] for item in events(client)]
    assert result.status_code == 200 and result.json()["status"] == "failed"
    assert "approved_action_failed" in types and "approved_action_succeeded" not in types


def test_unrestorable_record_is_refused_before_an_approval_is_requested(quarantine_client):
    app, client, root = quarantine_client
    suspect = root / "suspect.py"
    suspect.write_text("eval(input())", encoding="utf-8")
    moved = approve_over_http(client, lambda approval_id: client.post(
        "/health/security/quarantine", json={"file_path": "suspect.py", "reason": "eval"},
        params={"approval_id": approval_id} if approval_id else {}))
    suspect.write_text("a new file now occupies the original path", encoding="utf-8")
    before = len(client.get("/approvals").json()["approvals"])
    refused = client.post(f"/health/security/quarantined/{moved.json()['id']}/restore")
    assert refused.status_code == 404 and "already exists" in refused.json()["detail"]
    assert len(client.get("/approvals").json()["approvals"]) == before
    assert suspect.read_text(encoding="utf-8") == "a new file now occupies the original path"


def test_a_second_approval_for_the_same_proposal_applies_nothing():
    app, client = make_client()
    with client:
        created = client.post("/improvement/proposals", json={
            "target_name": "prompts/summarizer.md", "proposed_content": "Summarize once, briefly.", "rationale": "r"}).json()
        decide = f"/improvement/proposals/{created['id']}/decide"
        first = client.post(decide, json={"decision": "approved"}).json()["approval"]["approval_id"]
        client.post(f"/approvals/{first}/approve", json={})
        second = client.post(decide, json={"decision": "approved"}).json()["approval"]["approval_id"]
        client.post(f"/approvals/{second}/approve", json={})
        assert first != second
        assert client.post(decide, json={"decision": "approved"}, params={"approval_id": first}).status_code == 200
        again = client.post(decide, json={"decision": "approved"}, params={"approval_id": second})
    assert again.status_code == 422 and "already approved" in again.json()["detail"]


def test_store_refuses_use_outside_the_window_even_if_the_service_check_is_skipped():
    approvals = memory_approvals()
    request = approvals.request(OWNER, proposal())
    decided = approvals.decide(OWNER, request.approval_id, approve=True, note=None)
    decided_at = approval_module.datetime.fromisoformat(decided.decided_at)
    common = dict(category="quarantine", action="security.quarantine", scope_sha256=request.scope_sha256,
                  now=decided_at + timedelta(seconds=1))
    assert approvals.store.consume(OWNER, request.approval_id, decided_after=decided_at, **common) is None
    assert approvals.store.consume(OWNER, request.approval_id, decided_after=decided_at - timedelta(seconds=1),
                                   **common) is not None
