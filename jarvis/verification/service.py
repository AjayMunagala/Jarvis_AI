"""Verification Service coordinating planning, patch verification, and safe execution."""

import uuid
from pathlib import Path
from time import monotonic
from typing import Sequence

from jarvis.coding.models import PatchFile
from jarvis.core.config import Settings
from jarvis.repo.path_security import validate_repo_root
from jarvis.verification.models import (
    CheckResult,
    ProfileDefinition,
    VerificationPlan,
    VerificationPlanRequest,
    VerificationReport,
    VerificationState,
    VerifyPatchRequest,
)
from jarvis.verification.patch_verifier import PatchVerifier
from jarvis.verification.planner import VerificationPlanner
from jarvis.verification.runner import SafeProcessRunner


class VerificationService:
    """Core verification service for evidence-based validation and test planning."""

    def __init__(
        self,
        repo_root: Path | str,
        settings: Settings,
    ) -> None:
        self._root = validate_repo_root(repo_root)
        self._settings = settings
        self._planner = VerificationPlanner(self._root)
        self._patch_verifier = PatchVerifier(self._root, self._planner)
        self._runner = SafeProcessRunner(
            self._root,
            execution_enabled=settings.verification_execution_enabled,
            timeout_seconds=settings.verification_timeout_seconds,
            max_output_bytes=settings.verification_max_output_bytes,
        )
        self._plans: dict[str, VerificationPlan] = {}

    @property
    def repo_root(self) -> Path:
        return self._root

    @property
    def execution_enabled(self) -> bool:
        return self._settings.verification_execution_enabled

    def get_supported_profiles(self) -> tuple[ProfileDefinition, ...]:
        return self._runner.get_supported_profiles()

    def create_plan(self, request: VerificationPlanRequest) -> VerificationPlan:
        plan = self._planner.plan_for_paths(
            request.target_paths,
            reason=request.reason,
            execution_enabled=self.execution_enabled,
        )
        self._plans[plan.plan_id] = plan
        return plan

    def verify_patch(self, request: VerifyPatchRequest) -> VerificationReport:
        return self._patch_verifier.verify_patches(
            request.patches,
            reported_observation=request.reported_observation,
            execution_enabled=self.execution_enabled,
        )

    async def run_plan(
        self,
        plan_id: str,
        *,
        check_ids: Sequence[str] = (),
        deadline: float | None = None,
    ) -> VerificationReport:
        plan = self._plans.get(plan_id)
        if plan is None:
            # Reconstruct basic fallback plan if plan_id not in cache
            plan = self._planner.plan_for_paths([], reason="Execution of plan", execution_enabled=self.execution_enabled)

        checks_to_run = [
            c for c in plan.checks
            if not check_ids or c.check_id in check_ids
        ]

        results: list[CheckResult] = []
        facts: list[str] = []
        limitations: list[str] = []

        if not self.execution_enabled:
            limitations.append("Execution runner is disabled (verification_execution_enabled=False).")

        for check in checks_to_run:
            if deadline is not None and monotonic() >= deadline:
                # Deadline exceeded
                from datetime import datetime, timezone
                from jarvis.verification.models import VerificationEvidence
                now_str = datetime.now(timezone.utc).isoformat()
                ev = VerificationEvidence(
                    evidence_id=f"VE-{uuid.uuid4().hex[:10]}",
                    check_id=check.check_id,
                    check_name=check.name,
                    profile_id=check.profile_id,
                    state=VerificationState.CHECK_TIMED_OUT,
                    started_at=now_str,
                    completed_at=now_str,
                    duration_seconds=0.0,
                    output_excerpt="Overall verification request deadline exceeded before check execution started.",
                    redacted_error_details="Request deadline exceeded",
                    verified_files=check.expected_files,
                    provenance="direct_execution",
                    limitations=("Request deadline expired",),
                )
                results.append(
                    CheckResult(
                        check_id=check.check_id,
                        name=check.name,
                        scope=check.scope,
                        state=VerificationState.CHECK_TIMED_OUT,
                        evidence=ev,
                        summary=f"Check {check.name} timed out due to overall request deadline.",
                    )
                )
                continue

            result = await self._runner.execute_check(check)
            results.append(result)
            if result.state == VerificationState.CHECK_PASSED:
                facts.append(f"{check.name}: passed with exit code {result.evidence.exit_code}.")
            elif result.state == VerificationState.CHECK_FAILED:
                facts.append(f"{check.name}: failed with exit code {result.evidence.exit_code}.")

        # Determine overall report state
        if any(r.state == VerificationState.CHECK_FAILED for r in results):
            overall = VerificationState.CHECK_FAILED
            summary = "Verification completed with test failures."
        elif any(r.state == VerificationState.CHECK_TIMED_OUT for r in results):
            overall = VerificationState.CHECK_TIMED_OUT
            summary = "Verification timed out."
        elif any(r.state == VerificationState.CHECK_BLOCKED for r in results):
            overall = VerificationState.CHECK_BLOCKED
            summary = "Verification checks were blocked (execution disabled)."
        elif results and all(r.state == VerificationState.CHECK_PASSED for r in results):
            overall = VerificationState.CHECK_PASSED
            summary = "All executed verification checks passed."
        elif not results:
            overall = VerificationState.NOT_CHECKED
            summary = "No verification checks were executed."
        else:
            overall = VerificationState.INCONCLUSIVE
            summary = "Verification results were inconclusive."

        execution_id = f"exec-{uuid.uuid4().hex[:8]}"
        return VerificationReport(
            execution_id=execution_id,
            plan_id=plan.plan_id,
            overall_state=overall,
            results=tuple(results),
            summary=summary,
            facts=tuple(facts),
            limitations=tuple(limitations),
            execution_enabled=self.execution_enabled,
            repository_modified=False,
        )
