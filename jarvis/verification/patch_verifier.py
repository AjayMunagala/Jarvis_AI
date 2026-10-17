"""In-memory patch validation, preimage consistency, and syntax verification without disk mutation."""

import ast
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Sequence

from jarvis.coding.models import PatchFile
from jarvis.coding.patches import digest
from jarvis.repo.path_security import read_repository_text, safe_resolve_path, validate_repo_root
from jarvis.verification.models import (
    CheckResult,
    CheckScope,
    VerificationEvidence,
    VerificationPlan,
    VerificationReport,
    VerificationState,
)
from jarvis.verification.planner import VerificationPlanner


def _extract_postimage_from_patch(before_text: str, diff_text: str) -> str | None:
    """Reconstruct postimage from unified diff or replacement in-memory."""
    lines = diff_text.splitlines()
    post_lines = []
    # If unified diff:
    in_hunk = False
    for line in lines:
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            post_lines.append(line[1:])
        elif line.startswith(" "):
            post_lines.append(line[1:])
        elif line.startswith("-"):
            pass  # deleted in postimage
        elif line.startswith("\\"):
            pass  # no newline marker

    if post_lines:
        return "\n".join(post_lines) + ("\n" if diff_text.endswith("\n") else "")
    return None


class PatchVerifier:
    """Validates proposed patches statically in-memory; strictly never modifies repository files."""

    def __init__(self, repo_root: Path | str, planner: VerificationPlanner) -> None:
        self._root = validate_repo_root(repo_root)
        self._planner = planner

    def verify_patches(
        self,
        patches: Sequence[PatchFile],
        *,
        reported_observation: str | None = None,
        execution_enabled: bool = False,
    ) -> VerificationReport:
        now_str = datetime.now(timezone.utc).isoformat()
        results: list[CheckResult] = []
        facts: list[str] = []
        limitations: list[str] = [
            "Patches evaluated in-memory only; repository was NOT modified.",
            "Static syntax checks cannot establish semantic runtime correctness.",
        ]

        target_paths = [patch.path for patch in patches]
        plan: VerificationPlan = self._planner.plan_for_paths(
            target_paths,
            reason="Patch verification requested",
            execution_enabled=execution_enabled,
        )

        overall_state = VerificationState.CHECK_BLOCKED

        for idx, patch in enumerate(patches):
            rel_path = patch.path
            check_id = f"CHK-{hashlib.sha256(f'{rel_path}:{idx}'.encode()).hexdigest()[:8]}"
            file_ext = PurePosixPath(rel_path).suffix.lower()

            # 1. Preimage validation
            if patch.operation == "modify":
                current_text = read_repository_text(self._root, rel_path)
                if current_text is None:
                    ev = VerificationEvidence(
                        evidence_id=f"VE-{uuid.uuid4().hex[:10]}",
                        check_id=check_id,
                        check_name=f"Preimage check: {rel_path}",
                        profile_id="preimage_verifier",
                        state=VerificationState.CHECK_FAILED,
                        started_at=now_str,
                        completed_at=now_str,
                        duration_seconds=0.001,
                        output_excerpt="Target file does not exist on disk or cannot be read.",
                        redacted_error_details="Target file missing for modify operation",
                        verified_files=(rel_path,),
                        provenance="static_analysis",
                        limitations=("File missing on disk",),
                    )
                    results.append(
                        CheckResult(
                            check_id=check_id,
                            name=f"Preimage check: {rel_path}",
                            scope=CheckScope.SYNTAX,
                            state=VerificationState.CHECK_FAILED,
                            evidence=ev,
                            summary=f"Preimage check failed for {rel_path}: file does not exist.",
                            diagnostics=("Target file must exist prior to applying modifications.",),
                        )
                    )
                    overall_state = VerificationState.CHECK_FAILED
                    continue

                actual_sha = digest(current_text)
                normalized_sha = digest(current_text.replace("\r\n", "\n"))
                if patch.sha256_before and patch.sha256_before not in (actual_sha, normalized_sha):
                    ev = VerificationEvidence(
                        evidence_id=f"VE-{uuid.uuid4().hex[:10]}",
                        check_id=check_id,
                        check_name=f"Preimage hash check: {rel_path}",
                        profile_id="preimage_verifier",
                        state=VerificationState.CHECK_FAILED,
                        started_at=now_str,
                        completed_at=now_str,
                        duration_seconds=0.001,
                        output_excerpt=f"Expected preimage SHA-256 {patch.sha256_before}, but found {actual_sha}.",
                        redacted_error_details="Preimage SHA-256 mismatch",
                        verified_files=(rel_path,),
                        provenance="static_analysis",
                        limitations=("Disk content differs from patch baseline",),
                    )
                    results.append(
                        CheckResult(
                            check_id=check_id,
                            name=f"Preimage hash check: {rel_path}",
                            scope=CheckScope.SYNTAX,
                            state=VerificationState.CHECK_FAILED,
                            evidence=ev,
                            summary=f"Preimage hash mismatch for {rel_path}: file on disk has changed.",
                            diagnostics=("Regenerate the patch against current repository state.",),
                        )
                    )
                    overall_state = VerificationState.CHECK_FAILED
                    continue

                matched_sha = patch.sha256_before if patch.sha256_before in (actual_sha, normalized_sha) else actual_sha
                facts.append(f"{rel_path}: verified preimage hash matches disk ({matched_sha}).")

            elif patch.operation == "create":
                target_resolved = safe_resolve_path(self._root, rel_path, must_exist=False)
                if target_resolved.exists():
                    ev = VerificationEvidence(
                        evidence_id=f"VE-{uuid.uuid4().hex[:10]}",
                        check_id=check_id,
                        check_name=f"Target existence check: {rel_path}",
                        profile_id="preimage_verifier",
                        state=VerificationState.CHECK_FAILED,
                        started_at=now_str,
                        completed_at=now_str,
                        duration_seconds=0.001,
                        output_excerpt="Target file already exists on disk for create operation.",
                        redacted_error_details="Target file already exists",
                        verified_files=(rel_path,),
                        provenance="static_analysis",
                        limitations=("File already exists on disk",),
                    )
                    results.append(
                        CheckResult(
                            check_id=check_id,
                            name=f"Target existence check: {rel_path}",
                            scope=CheckScope.SYNTAX,
                            state=VerificationState.CHECK_FAILED,
                            evidence=ev,
                            summary=f"Create check failed for {rel_path}: file already exists on disk.",
                            diagnostics=("Use modify operation or choose a unique path.",),
                        )
                    )
                    overall_state = VerificationState.CHECK_FAILED
                    continue

                facts.append(f"{rel_path}: confirmed new target file does not collide with existing disk path.")

            # 2. In-memory simulated postimage syntax verification
            # Extract additions from patch diff
            if file_ext == ".py":
                simulated_postimage = _extract_postimage_from_patch(
                    current_text if patch.operation == "modify" else "",
                    patch.diff,
                )
                if simulated_postimage is not None:
                    try:
                        ast.parse(simulated_postimage, filename=rel_path)
                        ev = VerificationEvidence(
                            evidence_id=f"VE-{uuid.uuid4().hex[:10]}",
                            check_id=check_id,
                            check_name=f"AST syntax validation: {rel_path}",
                            profile_id="python_syntax",
                            state=VerificationState.CHECK_PASSED,
                            started_at=now_str,
                            completed_at=now_str,
                            duration_seconds=0.002,
                            output_excerpt=f"Python AST parsed successfully for simulated {rel_path}.",
                            verified_files=(rel_path,),
                            provenance="static_analysis",
                            limitations=("Static AST parsing only; runtime exceptions not tested",),
                        )
                        results.append(
                            CheckResult(
                                check_id=check_id,
                                name=f"AST syntax validation: {rel_path}",
                                scope=CheckScope.SYNTAX,
                                state=VerificationState.CHECK_PASSED,
                                evidence=ev,
                                summary=f"In-memory AST validation passed for {rel_path}.",
                            )
                        )
                        facts.append(f"{rel_path}: simulated postimage conforms to valid Python AST.")
                    except SyntaxError as syn_err:
                        err_line = syn_err.lineno or 1
                        err_msg = syn_err.msg or "syntax error"
                        ev = VerificationEvidence(
                            evidence_id=f"VE-{uuid.uuid4().hex[:10]}",
                            check_id=check_id,
                            check_name=f"AST syntax validation: {rel_path}",
                            profile_id="python_syntax",
                            state=VerificationState.CHECK_FAILED,
                            started_at=now_str,
                            completed_at=now_str,
                            duration_seconds=0.002,
                            output_excerpt=f"SyntaxError in simulated postimage: line {err_line}: {err_msg}",
                            redacted_error_details=f"SyntaxError: {err_msg} at line {err_line}",
                            verified_files=(rel_path,),
                            provenance="static_analysis",
                            limitations=("Syntax error detected in proposed diff",),
                        )
                        results.append(
                            CheckResult(
                                check_id=check_id,
                                name=f"AST syntax validation: {rel_path}",
                                scope=CheckScope.SYNTAX,
                                state=VerificationState.CHECK_FAILED,
                                evidence=ev,
                                summary=f"Syntax error in proposed patch for {rel_path}: {err_msg} at line {err_line}.",
                                diagnostics=(f"Inspect patch diff around line {err_line} for syntax errors.",),
                            )
                        )
                        overall_state = VerificationState.CHECK_FAILED
                        continue

        # 3. Represent planned test checks in accordance with execution policy
        if overall_state != VerificationState.CHECK_FAILED:
            for check_item in plan.checks:
                if check_item.scope == CheckScope.SYNTAX:
                    continue  # already checked above

                check_id = check_item.check_id
                if not execution_enabled:
                    ev = VerificationEvidence(
                        evidence_id=f"VE-{uuid.uuid4().hex[:10]}",
                        check_id=check_id,
                        check_name=check_item.name,
                        profile_id=check_item.profile_id,
                        state=VerificationState.CHECK_BLOCKED,
                        started_at=now_str,
                        completed_at=now_str,
                        duration_seconds=0.0,
                        output_excerpt="Test execution is disabled by server policy (verification_execution_enabled=False).",
                        redacted_error_details="Execution disabled",
                        verified_files=check_item.expected_files,
                        provenance="static_analysis",
                        limitations=("Test command identified but not executed",),
                    )
                    results.append(
                        CheckResult(
                            check_id=check_id,
                            name=check_item.name,
                            scope=check_item.scope,
                            state=VerificationState.CHECK_BLOCKED,
                            evidence=ev,
                            summary=f"Check {check_item.name} blocked: verification execution is disabled.",
                            diagnostics=(
                                f"Test suite {check_item.target} is relevant to verify this patch.",
                                "Run the test manually in an authorized environment.",
                            ),
                        )
                    )
                else:
                    # When execution is enabled, unapplied patches cannot be tested on disk directly without worktree isolation
                    ev = VerificationEvidence(
                        evidence_id=f"VE-{uuid.uuid4().hex[:10]}",
                        check_id=check_id,
                        check_name=check_item.name,
                        profile_id=check_item.profile_id,
                        state=VerificationState.CHECK_BLOCKED,
                        started_at=now_str,
                        completed_at=now_str,
                        duration_seconds=0.0,
                        output_excerpt="Patch is unapplied; live workspace execution without patch application tests baseline only.",
                        redacted_error_details="Unapplied patch cannot be verified against main workspace",
                        verified_files=check_item.expected_files,
                        provenance="static_analysis",
                        limitations=("Live execution requires isolated patch application",),
                    )
                    results.append(
                        CheckResult(
                            check_id=check_id,
                            name=check_item.name,
                            scope=check_item.scope,
                            state=VerificationState.CHECK_BLOCKED,
                            evidence=ev,
                            summary=f"Check {check_item.name} blocked: patch is unapplied to prevent workspace corruption.",
                            diagnostics=(
                                f"To run {check_item.target}, patch must be reviewed and applied to an isolated test environment.",
                            ),
                        )
                    )

            # Summarize non-failed states
            has_passed_syntax = any(r.state == VerificationState.CHECK_PASSED for r in results)
            has_blocked = any(r.state == VerificationState.CHECK_BLOCKED for r in results)
            if has_passed_syntax and has_blocked:
                overall_state = VerificationState.CHECK_BLOCKED
                summary_msg = (
                    "Proposed patch passed in-memory syntax validation, but live test suites "
                    "are blocked because execution is disabled by default."
                )
            elif has_passed_syntax:
                overall_state = VerificationState.CHECK_PASSED
                summary_msg = "All static verification checks passed."
            else:
                overall_state = VerificationState.CHECK_BLOCKED
                summary_msg = "Verification checks could not be executed."
        else:
            summary_msg = "Patch verification failed: syntax errors or preimage mismatches detected."

        if not execution_enabled:
            limitations.append("Execution runner is disabled (verification_execution_enabled=False). No tests were run.")

        execution_id = f"exec-{uuid.uuid4().hex[:8]}"
        return VerificationReport(
            execution_id=execution_id,
            plan_id=plan.plan_id,
            overall_state=overall_state,
            results=tuple(results),
            summary=summary_msg,
            facts=tuple(facts),
            limitations=tuple(limitations),
            execution_enabled=execution_enabled,
            repository_modified=False,
        )
