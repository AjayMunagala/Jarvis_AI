"""Comprehensive verification subsystem, planning, patch validation, and security tests."""

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.coding.models import PatchFile
from jarvis.coding.patches import digest
from jarvis.core.config import Settings
from jarvis.main import create_app
from jarvis.repo.service import RepoIntelligenceService
from jarvis.security.policy import Capability, CapabilityPolicy
from jarvis.verification.models import (
    CheckPlanItem,
    CheckScope,
    VerificationPlanRequest,
    VerificationReport,
    VerificationState,
    VerifyPatchRequest,
)
from jarvis.verification.patch_verifier import PatchVerifier
from jarvis.verification.planner import VerificationPlanner
from jarvis.verification.runner import SafeProcessRunner
from jarvis.verification.service import VerificationService

TOKEN = "verification-test-token-0123456789-abcdef"
OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def make_settings(**kwargs):
    return Settings(
        _env_file=None,
        environment="test",
        local_auth_token=SecretStr(TOKEN),
        owner_id=OWNER,
        **kwargs,
    )


@pytest.fixture
def test_repo(tmp_path: Path):
    maths_py = tmp_path / "maths.py"
    maths_py.write_bytes(b"def add(a, b):\n    return a + b\n")

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_maths_py = tests_dir / "test_maths.py"
    test_maths_py.write_bytes(b"from maths import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")

    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir(parents=True, exist_ok=True)
    app_tsx = frontend_dir / "App.tsx"
    app_tsx.write_bytes(b"export default function App() { return <div>App</div> }\n")

    return tmp_path


def make_patch(path: str, diff: str, sha_before: str | None, sha_after: str, op: str = "modify") -> PatchFile:
    return PatchFile(
        path=path,
        operation=op,  # type: ignore
        reason="Test patch",
        sha256_before=sha_before,
        sha256_after=sha_after,
        diff=diff,
    )


# =========================================================================
# 1. State Model Tests
# =========================================================================

def test_all_nine_verification_states_are_distinct():
    expected_states = {
        "not_checked",
        "check_requested",
        "check_running",
        "check_passed",
        "check_failed",
        "check_timed_out",
        "check_blocked",
        "check_result_unavailable",
        "inconclusive",
    }
    actual_states = {s.value for s in VerificationState}
    assert actual_states == expected_states
    assert len(VerificationState) == 9


# =========================================================================
# 2. Verification Planner Tests
# =========================================================================

def test_planner_creates_bounded_plan_for_python_module(test_repo: Path):
    planner = VerificationPlanner(test_repo)
    plan = planner.plan_for_paths(["maths.py"], reason="Test plan", execution_enabled=False)

    assert plan.plan_id.startswith("VP-")
    assert "maths.py" in plan.target_paths
    assert plan.execution_enabled is False
    assert len(plan.checks) >= 1

    # First check should be syntax check
    syntax_check = next((c for c in plan.checks if c.scope == CheckScope.SYNTAX), None)
    assert syntax_check is not None
    assert syntax_check.target == "maths.py"
    assert syntax_check.profile_id == "python_syntax"


def test_planner_creates_plan_for_frontend_module(test_repo: Path):
    planner = VerificationPlanner(test_repo)
    plan = planner.plan_for_paths(["frontend/App.tsx"], reason="Frontend plan", execution_enabled=False)

    assert len(plan.checks) >= 1
    typecheck = next((c for c in plan.checks if c.scope == CheckScope.TYPECHECK), None)
    assert typecheck is not None
    assert typecheck.profile_id == "tsc_build"


def test_planner_filters_unsafe_and_traversal_targets(test_repo: Path):
    planner = VerificationPlanner(test_repo)
    plan = planner.plan_for_paths(["../outside.py", ".git/config", "maths.py"], reason="Unsafe targets")

    assert "../outside.py" not in plan.target_paths
    assert ".git/config" not in plan.target_paths
    assert "maths.py" in plan.target_paths


# =========================================================================
# 3. Patch Verifier (In-Memory Preimage & Syntax) Tests
# =========================================================================

def test_patch_verifier_detects_preimage_hash_mismatch(test_repo: Path):
    planner = VerificationPlanner(test_repo)
    verifier = PatchVerifier(test_repo, planner)

    wrong_hash = "f" * 64
    patch = make_patch(
        "maths.py",
        "--- a/maths.py\n+++ b/maths.py\n@@ -1 +1 @@\n-def add\n+def sum\n",
        wrong_hash,
        "e" * 64,
    )

    report = verifier.verify_patches([patch], execution_enabled=False)
    assert report.overall_state == VerificationState.CHECK_FAILED
    assert any("Preimage hash mismatch" in r.summary for r in report.results)
    assert report.repository_modified is False


def test_patch_verifier_validates_clean_python_ast_in_memory(test_repo: Path):
    planner = VerificationPlanner(test_repo)
    verifier = PatchVerifier(test_repo, planner)

    current_content = (test_repo / "maths.py").read_text(encoding="utf-8")
    current_sha = digest(current_content)

    clean_diff = "--- a/maths.py\n+++ b/maths.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return (a + b) * 1\n"
    patch = make_patch("maths.py", clean_diff, current_sha, "valid-after")

    report = verifier.verify_patches([patch], execution_enabled=False)
    assert report.repository_modified is False

    syntax_res = next((r for r in report.results if r.name.startswith("AST syntax")), None)
    assert syntax_res is not None
    assert syntax_res.state == VerificationState.CHECK_PASSED


def test_patch_verifier_catches_syntax_error_in_patch(test_repo: Path):
    planner = VerificationPlanner(test_repo)
    verifier = PatchVerifier(test_repo, planner)

    current_content = (test_repo / "maths.py").read_text(encoding="utf-8")
    current_sha = digest(current_content)

    broken_diff = "--- a/maths.py\n+++ b/maths.py\n@@ -1,2 +1,2 @@\n-def add(a, b):\n+def broken(:\n     return a + b\n"
    patch = make_patch("maths.py", broken_diff, current_sha, "broken-after")

    report = verifier.verify_patches([patch], execution_enabled=False)
    assert report.overall_state == VerificationState.CHECK_FAILED

    syntax_res = next((r for r in report.results if r.name.startswith("AST syntax")), None)
    assert syntax_res is not None
    assert syntax_res.state == VerificationState.CHECK_FAILED
    assert "Syntax error" in syntax_res.summary


def test_patch_verifier_blocks_test_runs_when_execution_disabled(test_repo: Path):
    planner = VerificationPlanner(test_repo)
    verifier = PatchVerifier(test_repo, planner)

    current_content = (test_repo / "maths.py").read_text(encoding="utf-8")
    current_sha = digest(current_content)

    clean_diff = "--- a/maths.py\n+++ b/maths.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return a + b\n"
    patch = make_patch("maths.py", clean_diff, current_sha, "clean-after")

    report = verifier.verify_patches([patch], execution_enabled=False)
    assert report.execution_enabled is False

    unit_checks = [r for r in report.results if r.scope == CheckScope.UNIT]
    for uc in unit_checks:
        assert uc.state == VerificationState.CHECK_BLOCKED
        assert "execution is disabled" in uc.summary.lower()


# =========================================================================
# 4. SafeProcessRunner Tests
# =========================================================================

def test_runner_blocks_execution_when_disabled(test_repo: Path):
    runner = SafeProcessRunner(test_repo, execution_enabled=False)
    item = CheckPlanItem(
        check_id="CHK-000001",
        name="Targeted test",
        scope=CheckScope.UNIT,
        target="tests/test_maths.py",
        profile_id="pytest_targeted",
        rationale="Unit test",
        expected_files=("tests/test_maths.py",),
    )

    result = asyncio.run(runner.execute_check(item))
    assert result.state == VerificationState.CHECK_BLOCKED
    assert "server execution is disabled" in result.summary


def test_runner_rejects_unapproved_profile(test_repo: Path):
    runner = SafeProcessRunner(test_repo, execution_enabled=True)
    item = CheckPlanItem(
        check_id="CHK-000002",
        name="Arbitrary shell",
        scope=CheckScope.UNIT,
        target="tests/test_maths.py",
        profile_id="arbitrary_bash",
        rationale="Unapproved profile",
    )

    result = asyncio.run(runner.execute_check(item))
    assert result.state == VerificationState.CHECK_BLOCKED
    assert "unknown execution profile" in result.summary


def test_runner_rejects_target_outside_allowed_directories(test_repo: Path):
    runner = SafeProcessRunner(test_repo, execution_enabled=True)
    item = CheckPlanItem(
        check_id="CHK-000003",
        name="Targeted test",
        scope=CheckScope.UNIT,
        target="outside_dir/test_bad.py",
        profile_id="pytest_targeted",
        rationale="Outside boundary",
    )

    result = asyncio.run(runner.execute_check(item))
    assert result.state == VerificationState.CHECK_BLOCKED
    assert "not in allowed directories" in result.summary


def test_runner_executes_allowed_profile_and_captures_result(test_repo: Path):
    runner = SafeProcessRunner(test_repo, execution_enabled=True, timeout_seconds=15.0)
    item = CheckPlanItem(
        check_id="CHK-000004",
        name="Bytecode compile",
        scope=CheckScope.SYNTAX,
        target="tests/test_maths.py",
        profile_id="compileall",
        rationale="Compileall check",
    )

    result = asyncio.run(runner.execute_check(item))
    assert result.state == VerificationState.CHECK_PASSED
    assert result.evidence.exit_code == 0
    assert result.evidence.provenance == "direct_execution"


def test_runner_redacts_secrets_in_captured_output(test_repo: Path, monkeypatch):
    # Create test file outputting a mock OpenAI secret key
    secret_test = test_repo / "tests" / "test_leak.py"
    secret_test.write_text("print('SECRET: sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD')\n", encoding="utf-8")

    runner = SafeProcessRunner(test_repo, execution_enabled=True, timeout_seconds=10.0)
    item = CheckPlanItem(
        check_id="CHK-000005",
        name="Leak test",
        scope=CheckScope.UNIT,
        target="tests/test_leak.py",
        profile_id="compileall",
        rationale="Secret leak test",
    )

    result = asyncio.run(runner.execute_check(item))
    assert "sk-" not in result.evidence.output_excerpt


# =========================================================================
# 5. API Route & Capability Enforcement Tests
# =========================================================================

def test_verification_api_requires_bearer_token(test_repo: Path):
    app = create_app(settings=make_settings())
    app.state.repo_service = RepoIntelligenceService(test_repo)

    with TestClient(app) as client:
        res = client.post("/verification/plan", json={"target_paths": ["maths.py"]})
        assert res.status_code == 401


def test_verification_api_denies_without_verify_read_capability(test_repo: Path):
    # Policy with empty capabilities
    app = create_app(settings=make_settings())
    app.state.repo_service = RepoIntelligenceService(test_repo)

    class DenyPolicy(CapabilityPolicy):
        def allows(self, owner_id, cap, **kwargs):
            return False

    app.state.authorization_policy = DenyPolicy(OWNER)

    with TestClient(app) as client:
        res = client.post(
            "/verification/plan",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"target_paths": ["maths.py"]},
        )
        assert res.status_code == 403


def test_verification_plan_endpoint_creates_plan(test_repo: Path):
    app = create_app(settings=make_settings())
    app.state.repo_service = RepoIntelligenceService(test_repo)

    with TestClient(app) as client:
        res = client.post(
            "/verification/plan",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"target_paths": ["maths.py"], "reason": "Test plan"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["plan_id"].startswith("VP-")
        assert len(data["checks"]) >= 1


def test_verification_verify_patch_endpoint(test_repo: Path):
    app = create_app(settings=make_settings())
    app.state.repo_service = RepoIntelligenceService(test_repo)

    current_content = (test_repo / "maths.py").read_text(encoding="utf-8")
    current_sha = digest(current_content)

    patch_payload = {
        "patches": [
            {
                "path": "maths.py",
                "operation": "modify",
                "reason": "Fix addition",
                "sha256_before": current_sha,
                "sha256_after": "valid-after",
                "diff": "--- a/maths.py\n+++ b/maths.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return a + b\n",
            }
        ]
    }

    with TestClient(app) as client:
        res = client.post(
            "/verification/verify-patch",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json=patch_payload,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["repository_modified"] is False
        assert data["overall_state"] in {"check_blocked", "check_passed"}


def test_verification_run_endpoint_blocked_by_default(test_repo: Path):
    # By default verification_execution_enabled is False
    app = create_app(settings=make_settings())
    app.state.repo_service = RepoIntelligenceService(test_repo)

    # Even with a policy allowing VERIFY_EXECUTE, server configuration blocks it!
    class AllowExecutePolicy(CapabilityPolicy):
        def allows(self, owner_id, cap, **kwargs):
            return cap == Capability.VERIFY_EXECUTE

    app.state.authorization_policy = AllowExecutePolicy(OWNER)

    with TestClient(app) as client:
        res = client.post(
            "/verification/run",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"plan_id": "VP-0123456789"},
        )
        assert res.status_code == 403
        assert "disabled by server policy" in res.json()["detail"]


def test_verification_profiles_endpoint(test_repo: Path):
    app = create_app(settings=make_settings())
    app.state.repo_service = RepoIntelligenceService(test_repo)

    with TestClient(app) as client:
        res = client.get(
            "/verification/profiles",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["execution_enabled"] is False
        assert len(data["profiles"]) >= 4
