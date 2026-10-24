"""Safe subprocess execution interface for whitelisted verification profiles."""

import asyncio
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Sequence

from jarvis.repo.path_security import is_ignored_repository_path, safe_resolve_path, validate_repo_root
from jarvis.repo.secret_redactor import redact_secrets
from jarvis.verification.models import (
    CheckPlanItem,
    CheckResult,
    ProfileDefinition,
    VerificationEvidence,
    VerificationState,
)

SAFE_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
}

APPROVED_PROFILES: dict[str, ProfileDefinition] = {
    "pytest_targeted": ProfileDefinition(
        profile_id="pytest_targeted",
        name="Targeted Pytest",
        description="Runs pytest against targeted test files in tests/ or integration_tests/",
        working_directory=".",
        allowed_target_dirs=("tests", "integration_tests"),
        default_timeout_seconds=30.0,
    ),
    "compileall": ProfileDefinition(
        profile_id="compileall",
        name="Python Compile Verification",
        description="Bytecode compiles Python sources to verify syntax and import integrity",
        working_directory=".",
        allowed_target_dirs=("jarvis", "tests", "integration_tests"),
        default_timeout_seconds=15.0,
    ),
    "vitest_targeted": ProfileDefinition(
        profile_id="vitest_targeted",
        name="Targeted Vitest",
        description="Runs Vitest against targeted frontend test files",
        working_directory="frontend",
        allowed_target_dirs=("src", "frontend/src"),
        default_timeout_seconds=30.0,
    ),
    "tsc_build": ProfileDefinition(
        profile_id="tsc_build",
        name="TypeScript & Vite Build Check",
        description="Runs tsc -b and vite build in the frontend directory",
        working_directory="frontend",
        allowed_target_dirs=("frontend",),
        default_timeout_seconds=45.0,
    ),
    "playwright_targeted": ProfileDefinition(
        profile_id="playwright_targeted",
        name="Playwright E2E Tests",
        description="Runs Playwright browser verification workflows",
        working_directory="frontend",
        allowed_target_dirs=("frontend", "e2e"),
        default_timeout_seconds=60.0,
    ),
}


def _build_clean_environment() -> dict[str, str]:
    """Sanitize environment by stripping ambient overrides and hostile variables."""
    clean = {}
    for key, value in os.environ.items():
        key_upper = key.upper()
        if key_upper in SAFE_ENV_KEYS and not key_upper.startswith("GIT_") and not key_upper.startswith("PYTHON"):
            clean[key] = value
    return clean


class SafeProcessRunner:
    """Executes strictly whitelisted profiles with strict bounds, timeouts, and redaction."""

    def __init__(
        self,
        repo_root: Path | str,
        *,
        execution_enabled: bool = False,
        timeout_seconds: float = 30.0,
        max_output_bytes: int = 32768,
    ) -> None:
        self._root = validate_repo_root(repo_root)
        self._execution_enabled = execution_enabled
        self._timeout_seconds = max(1.0, min(120.0, timeout_seconds))
        self._max_output_bytes = max(1024, min(131072, max_output_bytes))

    @property
    def execution_enabled(self) -> bool:
        return self._execution_enabled

    def get_supported_profiles(self) -> tuple[ProfileDefinition, ...]:
        return tuple(APPROVED_PROFILES.values())

    async def execute_check(self, check: CheckPlanItem) -> CheckResult:
        now_str = datetime.now(timezone.utc).isoformat()
        evidence_id = f"VE-{uuid.uuid4().hex[:10]}"

        # 1. Gate: execution_enabled must be True
        if not self._execution_enabled:
            evidence = VerificationEvidence(
                evidence_id=evidence_id,
                check_id=check.check_id,
                check_name=check.name,
                profile_id=check.profile_id,
                state=VerificationState.CHECK_BLOCKED,
                started_at=now_str,
                completed_at=now_str,
                duration_seconds=0.0,
                output_excerpt="Execution is disabled by server policy (verification_execution_enabled=False).",
                redacted_error_details="Execution disabled by server policy",
                verified_files=check.expected_files,
                provenance="static_analysis",
                limitations=("Check was identified but command execution is disabled",),
            )
            return CheckResult(
                check_id=check.check_id,
                name=check.name,
                scope=check.scope,
                state=VerificationState.CHECK_BLOCKED,
                evidence=evidence,
                summary=f"Check {check.name} blocked: server execution is disabled.",
                diagnostics=(
                    f"Profile '{check.profile_id}' for target '{check.target}' was not executed.",
                    "Enable verification_execution_enabled in server configuration to allow authorized runs.",
                ),
            )

        # 2. Check profile allowlist
        profile = APPROVED_PROFILES.get(check.profile_id)
        if profile is None:
            evidence = VerificationEvidence(
                evidence_id=evidence_id,
                check_id=check.check_id,
                check_name=check.name,
                profile_id=check.profile_id,
                state=VerificationState.CHECK_BLOCKED,
                started_at=now_str,
                completed_at=now_str,
                duration_seconds=0.0,
                output_excerpt=f"Profile '{check.profile_id}' is not in approved verification allowlist.",
                redacted_error_details="Unknown or unapproved profile",
                verified_files=(),
                provenance="static_analysis",
                limitations=("Unapproved execution profile requested",),
            )
            return CheckResult(
                check_id=check.check_id,
                name=check.name,
                scope=check.scope,
                state=VerificationState.CHECK_BLOCKED,
                evidence=evidence,
                summary=f"Check {check.name} blocked: unknown execution profile '{check.profile_id}'.",
            )

        # 3. Construct structured command
        npm_cmd = "npm.cmd" if sys.platform == "win32" and shutil.which("npm.cmd") else "npm"
        python_exe = sys.executable

        cmd_args: list[str] = []
        cwd = self._root / profile.working_directory

        if check.profile_id == "pytest_targeted":
            cmd_args = [python_exe, "-m", "pytest", "-q", check.target]
        elif check.profile_id == "compileall":
            cmd_args = [python_exe, "-m", "compileall", "-q", check.target]
        elif check.profile_id == "vitest_targeted":
            # Target relative to frontend directory
            clean_target = check.target.replace("frontend/", "")
            cmd_args = [npm_cmd, "test", "--", "--run", clean_target]
        elif check.profile_id == "tsc_build":
            cmd_args = [npm_cmd, "run", "build"]
        elif check.profile_id == "playwright_targeted":
            cmd_args = [npm_cmd, "run", "test:e2e"]

        # Validate target path confinement
        if check.target and check.target != ".":
            norm_target = check.target.replace("\\", "/").strip("/")
            allowed = False
            for allowed_dir in profile.allowed_target_dirs:
                if norm_target == allowed_dir or norm_target.startswith(f"{allowed_dir}/"):
                    allowed = True
                    break
            if not allowed:
                evidence = VerificationEvidence(
                    evidence_id=evidence_id,
                    check_id=check.check_id,
                    check_name=check.name,
                    profile_id=check.profile_id,
                    state=VerificationState.CHECK_BLOCKED,
                    started_at=now_str,
                    completed_at=now_str,
                    duration_seconds=0.0,
                    output_excerpt=f"Target '{check.target}' is outside allowed directories for profile {profile.profile_id}.",
                    redacted_error_details="Target directory not permitted",
                    verified_files=(),
                    provenance="static_analysis",
                    limitations=("Target outside profile boundary",),
                )
                return CheckResult(
                    check_id=check.check_id,
                    name=check.name,
                    scope=check.scope,
                    state=VerificationState.CHECK_BLOCKED,
                    evidence=evidence,
                    summary=f"Check {check.name} blocked: target path not in allowed directories.",
                )

        # 4. Execute with timeout, process cleanup, and secret redaction
        clean_env = _build_clean_environment()
        timeout = min(self._timeout_seconds, profile.default_timeout_seconds)
        start_time = asyncio.get_event_loop().time()
        start_iso = datetime.now(timezone.utc).isoformat()

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                cwd=str(cwd),
                env=clean_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            async with asyncio.timeout(timeout):
                stdout_data, stderr_data = await proc.communicate()

            duration = round(asyncio.get_event_loop().time() - start_time, 3)
            end_iso = datetime.now(timezone.utc).isoformat()

            raw_output = (stdout_data or b"").decode("utf-8", errors="replace")
            raw_err = (stderr_data or b"").decode("utf-8", errors="replace")

            combined_output = raw_output
            if raw_err.strip():
                combined_output = f"{raw_output}\n--- stderr ---\n{raw_err}"

            # Redact secrets before saving evidence
            safe_output, _ = redact_secrets(combined_output)
            truncated = False
            if len(safe_output) > self._max_output_bytes:
                safe_output = safe_output[:self._max_output_bytes] + "\n[Output truncated: exceeded output budget]"
                truncated = True

            safe_err, _ = redact_secrets(raw_err) if raw_err else (None, False)

            exit_code = proc.returncode
            if exit_code == 0:
                state = VerificationState.CHECK_PASSED
                summary = f"Check {check.name} passed with exit code 0."
            else:
                state = VerificationState.CHECK_FAILED
                summary = f"Check {check.name} failed with exit code {exit_code}."

            evidence = VerificationEvidence(
                evidence_id=evidence_id,
                check_id=check.check_id,
                check_name=check.name,
                profile_id=check.profile_id,
                state=state,
                command_executed=tuple(cmd_args),
                exit_code=exit_code,
                started_at=start_iso,
                completed_at=end_iso,
                duration_seconds=duration,
                output_excerpt=safe_output[:16000],
                output_truncated=truncated,
                redacted_error_details=safe_err[:4000] if safe_err else None,
                verified_files=check.expected_files,
                provenance="direct_execution",
                limitations=(),
            )
            return CheckResult(
                check_id=check.check_id,
                name=check.name,
                scope=check.scope,
                state=state,
                evidence=evidence,
                summary=summary,
            )

        except (TimeoutError, asyncio.TimeoutError):
            duration = round(asyncio.get_event_loop().time() - start_time, 3)
            end_iso = datetime.now(timezone.utc).isoformat()
            if proc:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

            evidence = VerificationEvidence(
                evidence_id=evidence_id,
                check_id=check.check_id,
                check_name=check.name,
                profile_id=check.profile_id,
                state=VerificationState.CHECK_TIMED_OUT,
                command_executed=tuple(cmd_args),
                exit_code=None,
                started_at=start_iso,
                completed_at=end_iso,
                duration_seconds=duration,
                output_excerpt=f"Execution timed out after {timeout} seconds.",
                redacted_error_details="Process timed out",
                verified_files=check.expected_files,
                provenance="direct_execution",
                limitations=("Check execution exceeded wall-clock timeout budget",),
            )
            return CheckResult(
                check_id=check.check_id,
                name=check.name,
                scope=check.scope,
                state=VerificationState.CHECK_TIMED_OUT,
                evidence=evidence,
                summary=f"Check {check.name} timed out after {timeout}s.",
                diagnostics=("Increase timeout setting or narrow the targeted test scope.",),
            )

        except Exception as exc:
            duration = round(asyncio.get_event_loop().time() - start_time, 3)
            end_iso = datetime.now(timezone.utc).isoformat()
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass

            safe_err, _ = redact_secrets(str(exc))
            evidence = VerificationEvidence(
                evidence_id=evidence_id,
                check_id=check.check_id,
                check_name=check.name,
                profile_id=check.profile_id,
                state=VerificationState.CHECK_RESULT_UNAVAILABLE,
                command_executed=tuple(cmd_args),
                exit_code=None,
                started_at=start_iso,
                completed_at=end_iso,
                duration_seconds=duration,
                output_excerpt=f"Execution error encountered: {safe_err}",
                redacted_error_details=safe_err[:4000],
                verified_files=check.expected_files,
                provenance="direct_execution",
                limitations=("Process execution encountered an unhandled error",),
            )
            return CheckResult(
                check_id=check.check_id,
                name=check.name,
                scope=check.scope,
                state=VerificationState.CHECK_RESULT_UNAVAILABLE,
                evidence=evidence,
                summary=f"Check {check.name} resulted in unavailable outcome: {safe_err}.",
            )
