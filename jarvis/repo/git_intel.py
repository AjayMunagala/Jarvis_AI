"""Strictly read-only Git intelligence, status, and change analysis."""

import os
import shutil
import subprocess
from pathlib import Path

from jarvis.repo.models import GitDiffEntry, GitFileStatus, GitStatusSummary
from jarvis.repo.path_security import validate_repo_root
from jarvis.repo.secret_redactor import redact_secrets

MAX_DIFF_BYTES = 100 * 1024  # 100 KiB bound for diff content


def _run_git_cmd(repo_root: Path, args: list[str], timeout_seconds: float = 5.0) -> tuple[int, str, str]:
    """Execute a read-only git command with validated arguments and a timeout."""
    git_bin = shutil.which("git")
    if not git_bin:
        return -1, "", "git executable not found in PATH."

    # Strip any ambient Git environment overrides (e.g. GIT_DIR, GIT_WORK_TREE,
    # GIT_INDEX_FILE, GIT_OBJECT_DIRECTORY) that could redirect operations
    # outside the intended repo_root, while preserving essential system variables.
    safe_env = {
        key: val for key, val in os.environ.items()
        if not key.upper().startswith("GIT_")
    }

    try:
        proc = subprocess.run(
            [git_bin, "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
            cwd=str(repo_root),
            env=safe_env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -2, "", "Git command timed out."
    except Exception as exc:
        return -3, "", f"Failed to execute git: {exc}"


def get_git_status(repo_root: Path | str, *, max_diff_entries: int = 50) -> GitStatusSummary:
    """Analyze the Git working tree using read-only status and diff operations."""
    root = validate_repo_root(repo_root)

    # Verify if directory is a git repository
    rc, stdout, stderr = _run_git_cmd(root, ["rev-parse", "--is-inside-work-tree"])
    if rc != 0 or stdout.strip() != "true":
        return GitStatusSummary(
            is_repo=False,
            error=stderr.strip() or "Not a Git repository.",
        )

    # 1. Branch name
    rc, branch_out, _ = _run_git_cmd(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch_out.strip() if rc == 0 else None

    # 2. HEAD commit hash
    rc, head_out, _ = _run_git_cmd(root, ["rev-parse", "HEAD"])
    head_commit = head_out.strip() if rc == 0 else None

    # 3. Status porcelain v1
    rc, status_out, _ = _run_git_cmd(root, ["status", "--porcelain=v1"])
    staged_files: list[GitFileStatus] = []
    unstaged_files: list[GitFileStatus] = []
    untracked_files: list[str] = []
    changed_modules_set: set[str] = set()

    if rc == 0 and status_out:
        for line in status_out.splitlines():
            if len(line) < 4:
                continue
            index_status = line[0]
            worktree_status = line[1]
            file_path = line[3:].strip().strip('"')

            # Identify changed module/subsystem
            parts = Path(file_path).parts
            if len(parts) > 1:
                changed_modules_set.add(f"{parts[0]}/{parts[1]}")
            elif len(parts) == 1:
                changed_modules_set.add(parts[0])

            if index_status == "?" and worktree_status == "?":
                untracked_files.append(file_path)
            else:
                if index_status not in {" ", "?"}:
                    staged_files.append(
                        GitFileStatus(
                            path=file_path,
                            status_code=index_status,
                            is_staged=True,
                            is_untracked=False,
                            is_modified=index_status == "M",
                            is_deleted=index_status == "D",
                        )
                    )
                if worktree_status not in {" ", "?"}:
                    unstaged_files.append(
                        GitFileStatus(
                            path=file_path,
                            status_code=worktree_status,
                            is_staged=False,
                            is_untracked=False,
                            is_modified=worktree_status == "M",
                            is_deleted=worktree_status == "D",
                        )
                    )

    # 4. Diff entries (both unstaged and staged)
    diff_entries: list[GitDiffEntry] = []
    rc, diff_stat, _ = _run_git_cmd(root, ["diff", "--no-ext-diff", "--no-textconv", "--numstat", "HEAD"])
    if rc != 0:
        # Fallback to unstaged diff numstat if no commits exist
        rc, diff_stat, _ = _run_git_cmd(root, ["diff", "--no-ext-diff", "--no-textconv", "--numstat"])

    if rc == 0 and diff_stat:
        for line in diff_stat.splitlines()[:max_diff_entries]:
            tokens = line.split("\t")
            if len(tokens) == 3:
                ins, dels, path = tokens
                is_bin = ins == "-" or dels == "-"
                insertions = int(ins) if not is_bin else 0
                deletions = int(dels) if not is_bin else 0

                # Fetch file diff content with bounds and redaction
                _, file_diff, _ = _run_git_cmd(root, ["diff", "--no-ext-diff", "--no-textconv", "HEAD", "--", path])
                safe_diff, _ = redact_secrets(file_diff)
                truncated = False
                encoded_diff = safe_diff.encode("utf-8")
                if len(encoded_diff) > MAX_DIFF_BYTES:
                    safe_diff = encoded_diff[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore") + "\n... [diff truncated]"
                    truncated = True

                diff_entries.append(
                    GitDiffEntry(
                        path=path,
                        status="modified",
                        insertions=insertions,
                        deletions=deletions,
                        diff_content=safe_diff,
                        is_binary=is_bin,
                        is_truncated=truncated,
                    )
                )

    # 5. Determine potentially affected tests
    affected_tests = identify_affected_tests(
        [f.path for f in staged_files + unstaged_files] + untracked_files
    )

    return GitStatusSummary(
        is_repo=True,
        branch=branch,
        head_commit=head_commit,
        staged_files=staged_files,
        unstaged_files=unstaged_files,
        untracked_files=sorted(untracked_files),
        diff_entries=diff_entries,
        changed_modules=sorted(changed_modules_set),
        potentially_affected_tests=sorted(affected_tests),
    )


def identify_affected_tests(changed_files: list[str]) -> set[str]:
    """Map changed files to known relevant test suites."""
    affected: set[str] = set()

    for path in changed_files:
        norm = path.replace("\\", "/").lower()

        # If the changed file itself is a test
        if norm.startswith("tests/") or norm.startswith("integration_tests/"):
            affected.add(path)
            continue
        if ".test." in norm:
            affected.add(path)
            continue

        # Mapping application files to test suites
        if "jarvis/api/routes/conversations.py" in norm or "jarvis/api/routes/chat.py" in norm:
            affected.add("tests/test_api.py")
        if "jarvis/api/routes/health.py" in norm:
            affected.add("tests/test_health.py")
        if "jarvis/api/dependencies.py" in norm or "jarvis/security/" in norm:
            affected.add("tests/test_auth.py")
            affected.add("tests/test_policy.py")
        if "jarvis/persistence/" in norm:
            affected.add("tests/test_persistence.py")
            affected.add("integration_tests/test_postgres_integration.py")
        if "jarvis/memory/ingestion.py" in norm:
            affected.add("tests/test_memory_ingestion.py")
        if "jarvis/memory/" in norm:
            affected.add("tests/test_memory.py")
        if "jarvis/orchestration/" in norm:
            affected.add("tests/test_orchestration.py")
            affected.add("tests/test_verification.py")
        if "jarvis/intelligence/" in norm:
            affected.add("tests/test_intelligence_provider.py")
        if "jarvis/core/" in norm or "jarvis/main.py" in norm:
            affected.add("tests/test_api.py")
            affected.add("tests/test_health.py")
        if "frontend/src/app.tsx" in norm or "frontend/src/services/api.ts" in norm:
            affected.add("frontend/src/App.test.tsx")
            affected.add("frontend/src/services/api.test.ts")
            affected.add("frontend/e2e/jarvis.e2e.mjs")
        if "frontend/src/components/memorymanager.tsx" in norm:
            affected.add("frontend/src/components/MemoryManager.test.tsx")

    return affected
