"""Health and security service coordinating diagnostics, telemetry, and file quarantine (v3.6)."""

from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from jarvis.audit.models import ApprovalProposal
from jarvis.audit.service import run_approved
from jarvis.health.models import (
    DiagnosticReport,
    HealthSnapshot,
    HealthSnapshotPage,
    QuarantineRequest,
    QuarantinedFile,
    QuarantinedFilePage,
    SecretFinding,
)
from jarvis.health.probes import get_system_telemetry, scan_directory_for_secrets
from jarvis.health.quarantine import QuarantineManager
from jarvis.health.stores import HealthStore

logger = logging.getLogger(__name__)


class HealthService:
    def __init__(
        self,
        store: HealthStore,
        quarantine_dir: Path | None = None,
        repo_root: Path | None = None,
        audit_service: Any = None,
    ) -> None:
        self.store = store
        self.repo_root = repo_root or Path.cwd()
        self.approval_service: Any = None  # v4.3: quarantine and restore run only under an approved request
        q_dir = quarantine_dir or (self.repo_root / ".quarantine")
        self.quarantine_manager = QuarantineManager(q_dir, allowed_root=self.repo_root)
        self.audit_service = audit_service
        # v4.2: set by the application to StatusService.summary_map; snapshots used to record fixed values.
        self.status_provider = None

    def _audit(self, owner_id: UUID, event_type: str, subject: str, details: dict[str, Any]) -> None:
        if self.audit_service is not None:
            try:
                self.audit_service.record(
                    owner_id=owner_id,
                    category="health",
                    event_type=event_type,
                    decision="allowed",
                    status="succeeded",
                    subject=subject[:200],
                    details=details,
                )
            except Exception as exc:
                logger.warning("Failed to record health audit event: %s", exc)

    def capture_snapshot(self, owner_id: UUID) -> HealthSnapshot:
        telemetry = get_system_telemetry(self.repo_root)
        now = datetime.now(timezone.utc)
        services = {"status": "not_connected"}
        if self.status_provider is not None:
            try:
                services = dict(self.status_provider())
            except Exception:  # noqa: BLE001 - a failing status probe must not block the snapshot
                services = {"status": "unavailable"}
        snapshot = HealthSnapshot(
            id=uuid4(),
            owner_id=owner_id,
            cpu_percent=telemetry["cpu_percent"],
            memory_percent=telemetry["memory_percent"],
            disk_free_gb=telemetry["disk_free_gb"],
            services_status=services,
            created_at=now,
        )
        self.store.save_snapshot(snapshot)
        self._audit(
            owner_id,
            "health.snapshot.capture",
            f"health:snapshot:{snapshot.id}",
            {"disk_free_gb": snapshot.disk_free_gb, "cpu": snapshot.cpu_percent},
        )
        return snapshot

    def list_snapshots(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> HealthSnapshotPage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        items, total = self.store.list_snapshots(owner_id, limit=limit, offset=offset)
        return HealthSnapshotPage(items=items, total=total)

    def run_diagnostics(
        self, owner_id: UUID, scan_root: Path | None = None
    ) -> DiagnosticReport:
        target_root = scan_root or self.repo_root
        telemetry = self.capture_snapshot(owner_id)
        findings = scan_directory_for_secrets(target_root, max_findings=50)

        recommendations: list[str] = []
        if telemetry.disk_free_gb is None:
            recommendations.append("Disk space could not be measured.")
        elif telemetry.disk_free_gb < 5.0:
            recommendations.append("Warning: Disk space is critically low (< 5GB free).")
        else:
            recommendations.append("Disk space is adequate.")

        if findings:
            recommendations.append(
                f"Action Required: Found {len(findings)} potential secret exposures. Review and rotate credentials."
            )
        else:
            recommendations.append("No credential leaks detected in scanned directories.")

        self._audit(
            owner_id,
            "health.diagnostic.run",
            f"health:diagnostic:{telemetry.id}",
            {"secret_findings": len(findings), "disk_free_gb": telemetry.disk_free_gb},
        )

        return DiagnosticReport(
            telemetry=telemetry,
            secret_findings=findings,
            recommendations=recommendations,
        )

    def quarantine_file(
        self,
        owner_id: UUID,
        req: QuarantineRequest,
        approval_id: str | None = None,
    ) -> QuarantinedFile:
        """Quarantine one repository file once the owner approved exactly this file content (v4.3)."""
        resolved, file_hash = self.quarantine_manager.inspect(Path(req.file_path))
        display = self.quarantine_manager.display_path(resolved)
        proposal = ApprovalProposal(
            category="quarantine", action="security.quarantine", target=display[:300], reason=req.reason, risk="medium",
            expected_side_effects="Moves the file out of the repository into the quarantine directory; it can be restored.",
            scope_material=f"{display}|{file_hash}|{req.reason}",
        )
        return run_approved(self.approval_service, owner_id, proposal, approval_id,
                            lambda: self._quarantine(owner_id, req, resolved, file_hash))

    def _quarantine(self, owner_id: UUID, req: QuarantineRequest, resolved: Path, file_hash: str) -> QuarantinedFile:
        record = self.quarantine_manager.quarantine_file(
            owner_id=owner_id,
            file_path=resolved,
            reason=req.reason,
            approved_by=owner_id,
            expected_sha256=file_hash,
        )
        self.store.save_quarantined_file(record)
        self._audit(
            owner_id,
            "security.file.quarantine",
            f"security:quarantine:{record.id}",
            {"original_path": self.quarantine_manager.display_path(record.original_path)},
        )
        return record

    def restore_quarantined_file(
        self, owner_id: UUID, record_id: UUID, approval_id: str | None = None
    ) -> QuarantinedFile:
        record = self.store.get_quarantined_file(owner_id, record_id)
        if not record:
            raise ValueError(f"Quarantined record {record_id} not found.")
        if record.restored_at is not None:
            raise ValueError(f"Quarantined record {record_id} was already restored.")
        self.quarantine_manager.check_restorable(record)  # refuse before any approval is requested or used
        display = self.quarantine_manager.display_path(record.original_path)
        proposal = ApprovalProposal(
            category="quarantine", action="security.restore", target=display[:300],
            reason=f"Restore the quarantined file to {display}"[:500], risk="medium",
            expected_side_effects="Moves the quarantined file back to its original location; nothing is overwritten.",
            scope_material=f"{record.id}|{record.hash_sha256}|{display}",
        )
        return run_approved(self.approval_service, owner_id, proposal, approval_id, lambda: self._restore(owner_id, record))

    def _restore(self, owner_id: UUID, record: QuarantinedFile) -> QuarantinedFile:
        restored_record = self.quarantine_manager.restore_file(record)
        self.store.update_quarantined_file(restored_record)
        self._audit(
            owner_id,
            "security.file.restore",
            f"security:quarantine:{restored_record.id}",
            {"original_path": self.quarantine_manager.display_path(restored_record.original_path)},
        )
        return restored_record

    def list_quarantined_files(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> QuarantinedFilePage:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        items, total = self.store.list_quarantined_files(
            owner_id, limit=limit, offset=offset
        )
        return QuarantinedFilePage(items=items, total=total)
