"""Secure file quarantine and restoration manager (v3.6).

Safely isolates compromised or anomalous files into an isolated directory (.quarantine/)
with non-executable extensions and SHA-256 cryptographic verification.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
from uuid import UUID, uuid4

from jarvis.health.models import QuarantinedFile


def calculate_file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


class QuarantineManager:
    def __init__(self, quarantine_dir: Path) -> None:
        self.quarantine_dir = quarantine_dir
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)

    def quarantine_file(
        self,
        owner_id: UUID,
        file_path: Path,
        reason: str,
        approved_by: UUID | None = None,
    ) -> QuarantinedFile:
        """Moves suspect file to quarantine directory with a .quarantine.bak extension."""
        resolved = file_path.resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Source file to quarantine does not exist: {file_path}")
        if resolved.is_dir():
            raise ValueError(f"Cannot quarantine directories: {file_path}")

        file_hash = calculate_file_sha256(resolved)
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

    def restore_file(self, record: QuarantinedFile) -> QuarantinedFile:
        """Restores a quarantined file back to its original location."""
        src_path = Path(record.quarantine_path)
        dest_path = Path(record.original_path)

        if not src_path.exists():
            raise FileNotFoundError(f"Quarantined backup file missing: {src_path}")

        # Check hash before restore
        current_hash = calculate_file_sha256(src_path)
        if current_hash != record.hash_sha256:
            raise ValueError("Cryptographic hash mismatch. Quarantined file has been altered.")

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
