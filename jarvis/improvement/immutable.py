"""Immutable security file enforcement (v3.9).

Prevents self-improvement routines from modifying core security policies,
verification logic, database migrations, or project configurations.

v4.1: the denylist below is kept as a second layer, but targets are now
allowlisted first (``resolve_improvement_target``): only the registered
virtual prompt templates, or ``.md``/``.txt`` files inside the repository's
``prompts/`` directory resolved through the repository path guard. Absolute
paths, drive letters, ``..``, symlinks and every other file are refused.
"""

from pathlib import Path

from jarvis.repo.path_security import PathSecurityError, safe_resolve_path

PROMPTS_DIRECTORY = "prompts"
ALLOWED_FILE_SUFFIXES = frozenset({".md", ".txt"})

IMMUTABLE_PATTERNS = [
    "jarvis/security",
    "jarvis/verification",
    "database/scripts",
    "pyproject.toml",
    ".env",
    "jarvis/core/config.py",
]


class ImmutableSecurityFileError(ValueError):
    """Raised when a proposal attempts to modify a protected immutable security file."""


def assert_mutable_target(target_name: str) -> None:
    """Verifies that the target path does not match any protected security files."""
    cleaned = target_name.replace("\\", "/").strip().lower()

    if ".." in cleaned:
        raise ImmutableSecurityFileError("Path traversal ('..') is strictly prohibited.")

    for pattern in IMMUTABLE_PATTERNS:
        if cleaned.startswith(pattern) or pattern in cleaned:
            raise ImmutableSecurityFileError(
                f"Target '{target_name}' is a protected immutable security file and cannot be modified."
            )


class ImprovementTargetNotAllowedError(ImmutableSecurityFileError):
    """Raised when a target is not on the self-improvement allowlist."""


def resolve_improvement_target(repo_root: Path, target_name: str, virtual_targets) -> Path | None:
    """Return the on-disk path for an allowlisted target, or None for a virtual target.

    Raises ``ImprovementTargetNotAllowedError`` (a subclass of
    ``ImmutableSecurityFileError``) for anything else.
    """
    assert_mutable_target(target_name)
    if target_name in virtual_targets:
        return None
    normalized = target_name.replace("\\", "/").strip()
    parts = [part for part in normalized.split("/") if part]
    if (len(parts) < 2 or parts[0] != PROMPTS_DIRECTORY or Path(normalized).suffix.lower() not in ALLOWED_FILE_SUFFIXES
            or Path(normalized).is_absolute() or ":" in normalized):
        raise ImprovementTargetNotAllowedError(
            f"Target '{target_name}' is not an allowed improvement target. Only registered prompt templates and "
            f".md/.txt files under {PROMPTS_DIRECTORY}/ can be changed.")
    try:
        return safe_resolve_path(repo_root, normalized, must_exist=False)
    except PathSecurityError as exc:
        raise ImprovementTargetNotAllowedError(f"Target '{target_name}' is not an allowed improvement target.") from exc
