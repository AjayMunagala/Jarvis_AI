"""Bounded verification plan construction based on repository discovery."""

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Sequence

from jarvis.coding.patches import authorized_path
from jarvis.repo.git_intel import identify_affected_tests
from jarvis.repo.path_security import is_ignored_repository_path, safe_resolve_path, validate_repo_root
from jarvis.repo.test_discovery import discover_tests_and_build
from jarvis.verification.models import CheckPlanItem, CheckScope, VerificationPlan


def _generate_check_id(target: str, scope: str, index: int) -> str:
    digest = hashlib.sha256(f"{target}:{scope}:{index}".encode()).hexdigest()[:8]
    return f"CHK-{digest}"


class VerificationPlanner:
    """Formulates minimal, targeted verification plans from repository metadata."""

    def __init__(self, repo_root: Path | str) -> None:
        self._root = validate_repo_root(repo_root)

    def plan_for_paths(
        self,
        target_paths: Sequence[str],
        *,
        reason: str = "Verification plan requested",
        execution_enabled: bool = False,
    ) -> VerificationPlan:
        validated_targets: list[str] = []
        for path in target_paths:
            normalized = path.replace("\\", "/").strip("/")
            if not normalized or is_ignored_repository_path(normalized):
                continue
            try:
                safe_path = authorized_path(self._root, normalized)
                validated_targets.append(safe_path)
            except Exception:
                continue

        # If no explicit targets, discover tests across repository
        test_info = discover_tests_and_build(self._root)
        affected = identify_affected_tests(validated_targets)

        checks: list[CheckPlanItem] = []
        seen_targets: set[str] = set()

        for idx, target in enumerate(validated_targets[:5]):
            posix_path = PurePosixPath(target)
            ext = posix_path.suffix.lower()

            # 1. In-memory / static syntax validation check for Python
            if ext == ".py":
                check_id = _generate_check_id(target, "syntax", idx)
                checks.append(
                    CheckPlanItem(
                        check_id=check_id,
                        name=f"Syntax AST check: {posix_path.name}",
                        scope=CheckScope.SYNTAX,
                        target=target,
                        profile_id="python_syntax",
                        rationale=f"Validate Python AST syntax and compilation structure for {target}",
                        expected_files=(target,),
                    )
                )

            # 2. Targeted Unit test checks
            matching_tests = [
                t for t in affected
                if posix_path.stem in PurePosixPath(t).stem
                or (target in test_info.module_to_test_map and t in test_info.module_to_test_map[target])
            ]
            if not matching_tests and target in test_info.module_to_test_map:
                matching_tests = test_info.module_to_test_map[target]

            for test_file in matching_tests[:2]:
                if test_file in seen_targets:
                    continue
                seen_targets.add(test_file)
                is_frontend = test_file.startswith("frontend/")
                profile = "vitest_targeted" if is_frontend else "pytest_targeted"
                check_id = _generate_check_id(test_file, "unit", len(checks))
                checks.append(
                    CheckPlanItem(
                        check_id=check_id,
                        name=f"Targeted unit test: {PurePosixPath(test_file).name}",
                        scope=CheckScope.UNIT,
                        target=test_file,
                        profile_id=profile,
                        rationale=f"Execute targeted unit test suite {test_file} covering changes in {target}",
                        expected_files=(test_file,),
                    )
                )

            # 3. Frontend TypeScript checking / building
            if ext in {".ts", ".tsx", ".js", ".jsx"} or target.startswith("frontend/"):
                if "frontend_typecheck" not in seen_targets:
                    seen_targets.add("frontend_typecheck")
                    check_id = _generate_check_id("frontend", "typecheck", len(checks))
                    checks.append(
                        CheckPlanItem(
                            check_id=check_id,
                            name="Frontend type checking and build",
                            scope=CheckScope.TYPECHECK,
                            target="frontend",
                            profile_id="tsc_build",
                            rationale=f"Verify TypeScript types and Vite build integrity for frontend files including {target}",
                            expected_files=(target,),
                        )
                    )

        # 4. Fallback if no specific checks identified
        if not checks and test_info.test_files:
            default_test = test_info.test_files[0]
            check_id = _generate_check_id(default_test, "regression", len(checks))
            checks.append(
                CheckPlanItem(
                    check_id=check_id,
                    name=f"Baseline regression test: {PurePosixPath(default_test).name}",
                    scope=CheckScope.REGRESSION,
                    target=default_test,
                    profile_id="pytest_targeted",
                    rationale="Execute baseline test to verify environment stability",
                    expected_files=(default_test,),
                )
            )

        plan_id = f"VP-{uuid.uuid4().hex[:10]}"
        return VerificationPlan(
            plan_id=plan_id,
            target_paths=tuple(validated_targets),
            checks=tuple(checks[:8]),
            rationale=reason[:1000],
            execution_enabled=execution_enabled,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
