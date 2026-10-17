"""Immutable security file enforcement (v3.9).

Prevents self-improvement routines from modifying core security policies,
verification logic, database migrations, or project configurations.
"""

from pathlib import Path

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
