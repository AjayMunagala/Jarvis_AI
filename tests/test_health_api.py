"""API integration tests for the v3.6 System Health & Reliability routes.

Tests /health/* endpoints with FastAPI TestClient:
- Authentication & Capability enforcement (401, 403)
- Snapshot capture and listing
- Diagnostic runs with secret scanning
- File quarantine and restoration with approval gating
- 404 error handling
"""

from pathlib import Path
import tempfile
from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from approval_flow import approve_over_http
from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy

TEST_TOKEN = "health-test-token-at-least-32-chars"
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
    res = no_auth_client.get("/health/diagnostic/snapshots")
    assert res.status_code == 401

    res = no_auth_client.post("/health/diagnostic/snapshots")
    assert res.status_code == 401


def test_capability_enforcement(client):
    client.app_ref.state.authorization_policy = DenyPolicy({Capability.HEALTH_DIAGNOSTIC_RUN})

    # Read should succeed
    res = client.get("/health/diagnostic/snapshots")
    assert res.status_code == 200

    # Diagnostic run should fail (403)
    res = client.post("/health/diagnostic/run")
    assert res.status_code == 403


# ===================================================================
# 2. Telemetry Snapshots & Diagnostic Runs
# ===================================================================

def test_snapshots_and_diagnostics(client):
    # Capture snapshot
    snap_res = client.post("/health/diagnostic/snapshots")
    assert snap_res.status_code == 201
    snap = snap_res.json()
    assert snap["owner_id"] == str(TEST_OWNER)
    assert snap["cpu_percent"] >= 0.0

    # List snapshots
    list_res = client.get("/health/diagnostic/snapshots")
    assert list_res.status_code == 200
    assert list_res.json()["total"] == 1
    assert list_res.json()["items"][0]["id"] == snap["id"]

    # Run diagnostics
    diag_res = client.post("/health/diagnostic/run")
    assert diag_res.status_code == 200
    diag = diag_res.json()
    assert "telemetry" in diag
    assert len(diag["recommendations"]) > 0


# ===================================================================
# 3. File Quarantine & Restoration
# ===================================================================

def test_file_quarantine_and_restore_api(client):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        # v4.1: quarantine is confined to a root; use the temporary directory as that root.
        from jarvis.health.quarantine import QuarantineManager

        client.app_ref.state.health_service.quarantine_manager = QuarantineManager(
            tmp_path / ".quarantine", allowed_root=tmp_path)
        suspect = tmp_path / "anomalous_agent.py"
        suspect.write_text("import socket; s = socket.socket()", encoding="utf-8")

        # Quarantine suspect file
        payload = {
            "file_path": str(suspect),
            "reason": "Anomalous raw socket opening detected in untrusted script",
        }
        q_res = approve_over_http(client, lambda approval_id: client.post(
            "/health/security/quarantine", json=payload, params={"approval_id": approval_id} if approval_id else {}))
        assert q_res.status_code == 201
        record = q_res.json()
        record_id = record["id"]
        assert not suspect.exists()
        assert record["restored_at"] is None

        # List quarantined
        list_res = client.get("/health/security/quarantined")
        assert list_res.status_code == 200
        assert list_res.json()["total"] == 1

        # Restore file
        restore_res = approve_over_http(client, lambda approval_id: client.post(
            f"/health/security/quarantined/{record_id}/restore", params={"approval_id": approval_id} if approval_id else {}))
        assert restore_res.status_code == 200
        restored = restore_res.json()
        assert restored["restored_at"] is not None
        assert suspect.exists()

        # 404 for missing file
        missing_res = client.post(
            "/health/security/quarantine",
            json={"file_path": str(tmp_path / "non_existent.py"), "reason": "None"},
        )
        assert missing_res.status_code == 404

        # 404 for non-existent record restore
        assert client.post(f"/health/security/quarantined/{uuid4()}/restore").status_code == 404
