"""Validate structured proposals and construct diffs in memory only."""

import difflib
import hashlib
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

from jarvis.coding.models import FileChange, PatchFile
from jarvis.repo.path_security import is_ignored_repository_path, read_repository_text, safe_resolve_path
from jarvis.repo.secret_redactor import redact_secrets

MAX_SOURCE_BYTES = 64 * 1024
EDITABLE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".json", ".toml", ".md", ".txt", ".yaml", ".yml", ".css", ".html", ".sql"}


class PatchValidationError(ValueError):
    def __init__(self):
        super().__init__("The proposed patch could not be validated against authorized repository evidence.")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def private_coding_path(path: str) -> bool:
    """Additional coding-context exclusions, including common credential-file names."""
    parts = path.replace("\\", "/").casefold().split("/")
    name = parts[-1]
    return (bool(redact_secrets(path)[1]) or any(part.startswith(".") for part in parts)
            or name.startswith(("credentials.", "secrets.", "pgpass.", "id_rsa", "id_ed25519"))
            or name in {"credentials", "secrets", "pgpass"}
            or any(part in {"private", "credentials", "secrets"} for part in parts[:-1]))


def authorized_path(root: Path, value: str) -> str:
    path = value.replace("\\", "/")
    if (not path or path != path.strip() or any(unicodedata.category(char) == "Cc" for char in path)
        or PureWindowsPath(path).drive or PurePosixPath(path).is_absolute()
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or is_ignored_repository_path(path) or private_coding_path(path)):
        raise PatchValidationError()
    try:
        safe_resolve_path(root, path)
    except (OSError, ValueError):
        raise PatchValidationError() from None
    return path


def _diff_lines(text: str) -> list[str]:
    # Unified diffs use LF boundaries. str.splitlines would also split Unicode
    # separators, vertical tabs and bare CR characters inside source lines.
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def generate_patches(root: Path, changes: tuple[FileChange, ...], snapshots: dict[str, str], requested_paths: set[str]) -> tuple[PatchFile, ...]:
    """Check preimages and emit diffs; never write or invoke an apply command."""
    output = []
    seen = set()
    total = 0
    for change in changes:
        path = authorized_path(root, change.path)
        if path.casefold() in seen or PurePosixPath(path).suffix.lower() not in EDITABLE_SUFFIXES:
            raise PatchValidationError()
        seen.add(path.casefold())
        target = safe_resolve_path(root, path)
        if change.operation == "create":
            if path not in requested_paths or target.exists():
                raise PatchValidationError()
            before, after = "", change.content
        else:
            if path not in snapshots:
                raise PatchValidationError()
            before = snapshots[path]
            if read_repository_text(root, path, max_bytes=MAX_SOURCE_BYTES) != before:
                raise PatchValidationError()
            after = before
            for replacement in change.replacements:
                if after.count(replacement.old_text) != 1 or replacement.old_text == replacement.new_text:
                    raise PatchValidationError()
                after = after.replace(replacement.old_text, replacement.new_text, 1)
        if after == before or "\x00" in after or len(after.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise PatchValidationError()
        if any(redact_secrets(value)[1] for value in (before, after, change.reason)):
            raise PatchValidationError()
        lines = difflib.unified_diff(
            _diff_lines(before), _diff_lines(after),
            fromfile="/dev/null" if change.operation == "create" else "a/" + path,
            tofile="b/" + path,
        )
        diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
        total += len(diff)
        if not diff or len(diff) > 16000 or total > 24000:
            raise PatchValidationError()
        output.append(PatchFile(path=path, operation=change.operation, reason=change.reason,
                                sha256_before=None if change.operation == "create" else digest(before),
                                sha256_after=digest(after), diff=diff))
    return tuple(output)
