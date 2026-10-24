"""Secure file quarantine and restoration manager (v3.6).

Safely isolates compromised or anomalous files into an isolated directory (.quarantine/)
with non-executable extensions and SHA-256 cryptographic verification.

v4.1 containment: only regular files inside the configured root (the
repository) can be quarantined, resolved through the repository path guard
(no absolute paths outside the root, no '..', no symlink or reparse-point
components). The .git directory and the quarantine directory itself are
refused, and a restore never overwrites an existing file. The quarantine is
a move with a SHA-256 integrity check, not encryption.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
from uuid import UUID, uuid4

from jarvis.health.models import QuarantinedFile
from jarvis.repo.path_security import PathSecurityError, safe_resolve_path

_REFUSED_TOP_LEVEL = frozenset({".git"})


def calculate_file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


class QuarantineManager:
    def __init__(self, quarantine_dir: Path, allowed_root: Path) -> None:
        self.allowed_root = allowed_root.resolve(strict=True)
        self.quarantine_dir = quarantine_dir
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        self._quarantine_resolved = self.quarantine_dir.resolve()

    def _contained(self, candidate: Path, *, must_exist: bool) -> Path:
        resolved = safe_resolve_path(self.allowed_root, candidate, must_exist=must_exist)
        relative = resolved.relative_to(self.allowed_root)
        if not relative.parts:
            raise PathSecurityError("The repository root itself cannot be quarantined.")
        if relative.parts[0].casefold() in _REFUSED_TOP_LEVEL:
            raise PathSecurityError("Files inside .git cannot be quarantined.")
        if resolved == self._quarantine_resolved or self._quarantine_resolved in resolved.parents:
            raise PathSecurityError("Files inside the quarantine directory cannot be quarantined again.")
        return resolved

    def display_path(self, path: str | Path) -> str:
        """Repository-relative form for messages and audit (never an absolute server path)."""
        try:
            return Path(path).resolve().relative_to(self.allowed_root).as_posix()
        except ValueError:
            return Path(path).name

    def inspect(self, file_path: Path) -> tuple[Path, str]:
        """Validate a quarantine target and return its resolved path and SHA-256, without moving it."""
        candidate = file_path
        if file_path.is_absolute():
            try:  # absolute paths are accepted only when they are inside the root
                candidate = file_path.relative_to(self.allowed_root)
            except ValueError as exc:
                raise PathSecurityError("Path escapes the approved repository root.") from exc
        resolved = self._contained(candidate, must_exist=False)
        if not resolved.exists():
            raise FileNotFoundError("The file to quarantine does not exist inside the repository.")
        if not resolved.is_file():
            raise ValueError("Only regular files can be quarantined.")
        return resolved, calculate_file_sha256(resolved)

    def quarantine_file(
        self,
        owner_id: UUID,
        file_path: Path,
        reason: str,
        approved_by: UUID | None = None,
        expected_sha256: str | None = None,
    ) -> QuarantinedFile:
        """Moves suspect file to quarantine directory with a .quarantine.bak extension."""
        resolved, file_hash = self.inspect(file_path)
        if expected_sha256 is not None and file_hash != expected_sha256:
            raise ValueError("The file changed after it was approved for quarantine; it was not moved.")
        record_id = uuid4()
        dest_filename = f"{record_id.hex}_{resolved.name}.quarantine.bak"
        dest_path = self.quarantine_dir / dest_filename

        # Move file safely
        shutil.move(str(resolved), str(dest_path))

        now = datetime.now(timezone.utc)
        return QuarantinedFile(
            id=record_id,
            owner_id=owner_id,
            original_path=str(resolved),
            quarantine_path=str(dest_path),
            reason=reason,
            hash_sha256=file_hash,
            approved_by=approved_by,
            quarantined_at=now,
            restored_at=None,
        )

    def check_restorable(self, record: QuarantinedFile) -> Path:
        """Non-mutating restore precheck; returns the contained destination or raises (v4.3)."""
        src_path = Path(record.quarantine_path)
        dest_path = Path(record.original_path)

        if not src_path.exists():
            raise FileNotFoundError("The quarantined copy is missing.")
        try:
            relative = dest_path.resolve().relative_to(self.allowed_root)
        except ValueError as exc:
            raise PathSecurityError("The recorded original location is outside the repository root.") from exc
        dest_path = self._contained(relative, must_exist=False)
        if dest_path.exists():
            raise ValueError("A file already exists at the original location; it was not overwritten.")

        # Check hash before restore
        current_hash = calculate_file_sha256(src_path)
        if current_hash != record.hash_sha256:
            raise ValueError("Cryptographic hash mismatch. Quarantined file has been altered.")
        return dest_path

    def restore_file(self, record: QuarantinedFile) -> QuarantinedFile:
        """Restores a quarantined file back to its original location."""
        src_path = Path(record.quarantine_path)
        dest_path = self.check_restorable(record)  # checked again: the state may change after approval

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dest_path))

        now = datetime.now(timezone.utc)
        return QuarantinedFile(
            id=record.id,
            owner_id=record.owner_id,
            original_path=record.original_path,
            quarantine_path=record.quarantine_path,
            reason=record.reason,
            hash_sha256=record.hash_sha256,
            approved_by=record.approved_by,
            quarantined_at=record.quarantined_at,
            restored_at=now,
        )
