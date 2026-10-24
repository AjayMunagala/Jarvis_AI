"""End-to-end integration tests for the v4.0 Autonomous AI Operating System Capstone.

Validates the unified autonomous coordinator, cross-subsystem dispatch,
capability-based access control, system overview, and command execution:
1. Authentication & Capability enforcement (401, 403)
2. Financial health assessment execution (finance + assistant)
3. Research & knowledge ingestion execution (research + knowledge + assistant)
4. Proactive maintenance sweep execution (health + improvement + assistant)
5. Proactive scheduling execution (scheduler + assistant)
6. Dry run simulation mode (dry_run=True)
7. System overview telemetry aggregation across all 8 subsystems
8. Autonomous command history
"""

from uuid import UUID
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from approval_flow import approve_over_http
from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy

TEST_TOKEN = "autonomous-os-capstone-test-token-32-chars"
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
    no_auth = TestClient(client.app_ref)
    res = no_auth.get("/autonomous/overview")
    assert res.status_code == 401

    res = no_auth.post(
        "/autonomous/execute",
        json={"prompt": "Review my financial runway", "dry_run": False},
    )
    assert res.status_code == 401


def test_capability_enforcement(client):
    client.app_ref.state.authorization_policy = DenyPolicy({Capability.AUTONOMOUS_EXECUTE})

    # Read overview should succeed
    res = client.get("/autonomous/overview")
    assert res.status_code == 200

    # Execute should fail with 403
    res = client.post(
        "/autonomous/execute",
        json={"prompt": "Review my financial runway", "dry_run": False},
    )
    assert res.status_code == 403


# ===================================================================
# 2. System Overview Telemetry (All 8 Subsystems)
# ===================================================================

def test_system_overview_operational_status(client):
    res = client.get("/autonomous/overview")
    assert res.status_code == 200
    overview = res.json()

    assert overview["version"] == "4.0.0"
    assert overview["system_status"] == "operational"

    subsystems = overview["subsystems"]
    expected_subsystems = [
        "assistant",
        "workflow",
        "knowledge",
        "research",
        "scheduler",
        "health",
        "finance",
        "improvement",
    ]
    for sub in expected_subsystems:
        assert sub in subsystems
        assert subsystems[sub]["operational"] is True

    assert "autonomous_os_capstone" in overview["active_features"]


# ===================================================================
# 3. Dry Run Simulation Mode
# ===================================================================

def test_dry_run_simulation(client):
    payload = {
        "prompt": "Conduct research on autonomous multi-agent systems and ingest document",
        "dry_run": True,
        "max_steps": 5,
    }
    res = client.post("/autonomous/execute", json=payload)  # a dry run writes nothing, so needs no approval
    assert res.status_code == 200
    result = res.json()

    assert result["status"] == "dry_run"
    assert result["intent"] == "RESEARCH_AND_KNOWLEDGE_INGEST"
    assert len(result["steps"]) >= 2
    for step in result["steps"]:
        assert step["status"] == "skipped"
        assert "[Dry Run]" in step["summary"]


# ===================================================================
# 4. Multi-Subsystem Command: Financial Health Assessment
# ===================================================================

def test_financial_health_assessment_command(client):
    # First seed a checking account via finance API
    client.post(
        "/finance/accounts",
        json={"name": "Primary Checking", "account_type": "checking", "institution": "Bank", "balance": 15000.0},
    )

    payload = {
        "prompt": "Evaluate my current financial health, net worth, and monthly budget runway",
        "dry_run": False,
        "max_steps": 5,
    }
    res = approve_over_http(client, lambda approval_id: client.post(
        "/autonomous/execute", json={**payload, **({"approval_id": approval_id} if approval_id else {})}))
    assert res.status_code == 200
    result = res.json()

    assert result["status"] == "completed"
    assert result["intent"] == "FINANCIAL_HEALTH_ASSESSMENT"
    assert "finance" in result["subsystems_involved"]
    assert "assistant" in result["subsystems_involved"]

    # Verify steps succeeded
    assert len(result["steps"]) == 3
    actions = [s["action"] for s in result["steps"]]
    assert "accounts.list" in actions
    assert "snapshot.generate" in actions
    assert "notes.create" in actions

    # Verify an Assistant note was created
    notes_res = client.get("/assistant/notes")
    assert notes_res.status_code == 200
    notes = notes_res.json()["items"]
    assert any("Autonomous Financial Review" in n["title"] for n in notes)


# ===================================================================
# 5. Multi-Subsystem Command: Research & Knowledge Ingestion
# ===================================================================

def test_research_and_knowledge_ingestion_command(client):
    payload = {
        "prompt": "Deep research analysis on transformer architecture scaling laws",
        "context": {
            "text_content": "Transformer scaling laws demonstrate power-law improvements with parameter count and compute."
        },
        "dry_run": False,
    }
    res = approve_over_http(client, lambda approval_id: client.post(
        "/autonomous/execute", json={**payload, **({"approval_id": approval_id} if approval_id else {})}))
    assert res.status_code == 200
    result = res.json()

    assert result["status"] == "completed"
    assert result["intent"] == "RESEARCH_AND_KNOWLEDGE_INGEST"
    assert set(result["subsystems_involved"]) >= {"research", "knowledge", "assistant"}

    # Verify steps
    step_actions = [s["action"] for s in result["steps"]]
    assert "documents.parse" in step_actions
    assert "chunks.ingest" in step_actions
    assert "tasks.create" in step_actions

    # Verify knowledge document exists
    k_res = client.get("/knowledge/documents")
    assert k_res.status_code == 200
    assert k_res.json()["total"] >= 1

    # Verify follow-up task exists in Assistant
    t_res = client.get("/assistant/tasks")
    assert t_res.status_code == 200
    tasks = t_res.json()["items"]
    assert any("Review research findings" in t["title"] for t in tasks)


# ===================================================================
# 6. Multi-Subsystem Command: Proactive Maintenance Sweep
# ===================================================================

def test_proactive_maintenance_sweep_command(client):
    payload = {
        "prompt": "Run full system diagnostic telemetry and benchmark check",
        "dry_run": False,
    }
    res = approve_over_http(client, lambda approval_id: client.post(
        "/autonomous/execute", json={**payload, **({"approval_id": approval_id} if approval_id else {})}))
    assert res.status_code == 200
    result = res.json()

    assert result["status"] == "completed"
    assert result["intent"] == "PROACTIVE_MAINTENANCE_SWEEP"
    assert "health" in result["subsystems_involved"]
    assert "improvement" in result["subsystems_involved"]

    step_actions = [s["action"] for s in result["steps"]]
    assert "diagnostic.run" in step_actions
    assert "benchmarks.run" in step_actions
    assert "notes.create" in step_actions


# ===================================================================
# 7. Multi-Subsystem Command: Proactive Scheduling
# ===================================================================

def test_proactive_scheduling_command(client):
    payload = {
        "prompt": "Schedule a proactive recurring maintenance reminder job",
        "dry_run": False,
    }
    res = approve_over_http(client, lambda approval_id: client.post(
        "/autonomous/execute", json={**payload, **({"approval_id": approval_id} if approval_id else {})}))
    assert res.status_code == 200
    result = res.json()

    assert result["status"] == "completed"
    assert result["intent"] == "PROACTIVE_SCHEDULING"
    assert "scheduler" in result["subsystems_involved"]

    # Verify scheduled job was registered
    jobs_res = client.get("/scheduler/jobs")
    assert jobs_res.status_code == 200
    assert jobs_res.json()["total"] >= 1


# ===================================================================
# 8. Command History Tracking
# ===================================================================

def test_command_history_retrieval(client):
    # Execute a live command and a dry-run command
    sweep = {"prompt": "Run full system diagnostic telemetry and benchmark check", "dry_run": False}
    approve_over_http(client, lambda approval_id: client.post(
        "/autonomous/execute", json={**sweep, **({"approval_id": approval_id} if approval_id else {})}))
    client.post("/autonomous/execute", json={"prompt": "Deep research analysis on agent systems", "dry_run": True})

    res = client.get("/autonomous/history")
    assert res.status_code == 200
    history = res.json()
    assert isinstance(history, list)
    assert len(history) == 2
    # All commands must belong to the test owner
    for cmd in history:
        assert cmd["owner_id"] == str(TEST_OWNER)
    assert history[0]["status"] == "completed"
    assert history[1]["status"] == "dry_run"
