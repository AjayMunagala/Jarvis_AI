"""Strict path security and boundary validation for repository access."""

import fnmatch
import re
import stat
from pathlib import Path, PureWindowsPath

WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})

# Prohibit alternate data streams (colon) and Windows drive letters in relative segments
INVALID_CHARACTERS = re.compile(r'[\x00-\x1f\x7f<>:"|?*]')

DEFAULT_IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
    ".venv-dev", "dist", "build", "out", "target", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".vite",
    ".next", "coverage", ".idea", ".vscode", "tmp", "temp",
})
DEFAULT_IGNORED_PATTERNS = frozenset({
    "*.pyc", "*.pyo", "*.pyd", "*.so", "*.dll", "*.exe", ".DS_Store",
    "Thumbs.db", ".env", ".env.*", "*.log", "*.swp", "*~",
})


def is_ignored_repository_path(relative_path: str) -> bool:
    """Apply the same exclusions to discovery and explicit file requests."""
    parts = relative_path.replace("\\", "/").split("/")
    return (
        any(part.startswith(".") or part.casefold() in DEFAULT_IGNORED_DIRS for part in parts[:-1])
        or (parts[-1].casefold() != ".env.example" and any(
            fnmatch.fnmatch(parts[-1].casefold(), pattern.casefold()) for pattern in DEFAULT_IGNORED_PATTERNS
        ))
    )


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
    Windows alternate data streams, drive-relative paths, and disallowed
    symbolic-link traversal.
    The component precheck narrows symlink races but cannot eliminate changes
    made concurrently by another process; resolved-path containment is checked
    again before returning the path.
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
        if part not in {".", ".."} and part.endswith((".", " ")):
            raise PathSecurityError("Trailing dots or spaces are forbidden in repository paths.")
        base_name = part.split(".")[0].upper()
        if base_name in WINDOWS_RESERVED_NAMES:
            raise PathSecurityError(f"Path contains reserved Windows device name: {part}")
        if ":" in part:
            raise PathSecurityError("Alternate data streams or colons in relative paths are forbidden.")
        if INVALID_CHARACTERS.search(part):
            raise PathSecurityError("Invalid characters in repository path.")

    # Resolve against root. Inspect each lexical component with lstat first:
    # Path.resolve() follows symlinks, so checking the resolved path itself
    # cannot tell whether the caller's path traversed one.
    resolved_root = repo_root.resolve(strict=True)
    target = (resolved_root / candidate)
    # Reject off-root absolute/UNC paths before any target inspection or resolution.
    try:
        relative_target = target.relative_to(resolved_root)
    except ValueError as exc:
        raise PathSecurityError("Path escapes the approved repository root.") from exc

    if not allow_symlinks:
        if relative_target is not None:
            component_path = resolved_root
            for component in relative_target.parts:
                component_path = component_path / component
                try:
                    component_stat = component_path.lstat()
                except FileNotFoundError:
                    # Nonexistent suffixes are handled by the strict resolve
                    # below when must_exist=True.
                    continue
                except OSError as exc:
                    raise PathSecurityError("Unable to inspect the requested path safely.") from exc
                if stat.S_ISLNK(component_stat.st_mode) or (
                    getattr(component_stat, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise PathSecurityError(
                        "Symbolic links are disallowed in read-only repository inspection (including reparse points)."
                    )

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

    return resolved_target


def to_relative_posix(repo_root: Path, target_path: Path) -> str:
    """Validate the original path before converting it to a relative name."""
    resolved_root = repo_root.resolve()
    try:
        relative = target_path.relative_to(resolved_root) if target_path.is_absolute() else target_path
    except ValueError as exc:
        raise PathSecurityError("Path escapes the approved repository root.") from exc
    resolved = safe_resolve_path(resolved_root, relative)
    return resolved.relative_to(resolved_root).as_posix()


def read_repository_text(
    repo_root: Path, relative_path: str, *, max_bytes: int = 512 * 1024,
) -> str | None:
    """Read bounded ordinary text only after original-path validation.

    Unsafe, excluded, missing, oversized, and inaccessible files are omitted.
    This is a precheck, not protection against concurrent filesystem changes.
    """
    if is_ignored_repository_path(relative_path):
        return None
    try:
        root = validate_repo_root(repo_root)
        path = safe_resolve_path(root, relative_path, must_exist=True)
        if is_ignored_repository_path(path.relative_to(root).as_posix()):
            return None
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            return None
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None
        return raw.decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None
