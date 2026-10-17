"""Strict path security and boundary validation for repository access."""

import os
import re
from pathlib import Path, PureWindowsPath

WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})

# Prohibit alternate data streams (colon) and Windows drive letters in relative segments
INVALID_CHARACTERS = re.compile(r'[\x00-\x1f\x7f<>:"|?*]')


class PathSecurityError(ValueError):
    """Raised when a path breaches repository boundary or security constraints."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.public_message = message


def validate_repo_root(root: Path | str) -> Path:
    """Validate and resolve the repository root directory."""
    if not root:
        raise PathSecurityError("Repository root path must not be empty.")
    try:
        resolved = Path(root).resolve(strict=True)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise PathSecurityError(f"Repository root directory is invalid or not accessible: {exc}") from exc

    if not resolved.is_dir():
        raise PathSecurityError(f"Repository root is not a directory: {resolved}")
    return resolved


def safe_resolve_path(
    repo_root: Path,
    candidate: Path | str,
    *,
    must_exist: bool = False,
    allow_symlinks: bool = False,
) -> Path:
    """Safely resolve a candidate path within an approved repository root.

    Prevents directory traversal ('..'), absolute paths outside the root,
    Windows alternate data streams, drive-relative paths, and symlink escapes.
    """
    if candidate is None:
        raise PathSecurityError("Target path must not be None.")

    raw_str = str(candidate).strip()
    if not raw_str:
        raise PathSecurityError("Target path must not be empty.")

    if "\x00" in raw_str:
        raise PathSecurityError("Path must not contain NUL bytes.")

    # Check for drive-relative paths on Windows (e.g., 'C:foo')
    win_path = PureWindowsPath(raw_str)
    if win_path.drive and not win_path.root:
        raise PathSecurityError("Drive-relative paths are forbidden.")

    # Check for alternate data streams (colon in path other than drive specifier)
    normalized = raw_str.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p]

    # Verify no traversal elements before path resolution
    if any(p == ".." for p in parts):
        raise PathSecurityError("Path traversal ('..') is strictly prohibited.")

    for part in parts:
        base_name = part.split(".")[0].upper()
        if base_name in WINDOWS_RESERVED_NAMES:
            raise PathSecurityError(f"Path contains reserved Windows device name: {part}")
        if ":" in part:
            raise PathSecurityError("Alternate data streams or colons in relative paths are forbidden.")

    # Resolve against root
    resolved_root = repo_root.resolve(strict=True)
    target = (resolved_root / candidate)

    try:
        resolved_target = target.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        if must_exist:
            raise PathSecurityError(f"Path does not exist: {candidate}") from exc
        resolved_target = target.resolve()
    except (OSError, RuntimeError) as exc:
        raise PathSecurityError(f"Failed to resolve path safely: {exc}") from exc

    # Enforce containment within repo_root
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise PathSecurityError("Resolved path escapes the approved repository root.") from exc

    # Symlink verification
    if not allow_symlinks and resolved_target.is_symlink():
        raise PathSecurityError("Symbolic links are disallowed in read-only repository inspection.")

    return resolved_target


def to_relative_posix(repo_root: Path, target_path: Path) -> str:
    """Convert a target path to a canonical repository-relative POSIX string."""
    resolved_root = repo_root.resolve()
    try:
        relative = target_path.resolve().relative_to(resolved_root)
        return relative.as_posix()
    except ValueError:
        # Fallback if not resolved
        return target_path.as_posix()
