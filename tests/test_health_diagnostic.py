"""Unit tests for the v3.6 System Health & Reliability subsystem."""

from pathlib import Path
import tempfile
from uuid import uuid4
import pytest

from jarvis.health.models import QuarantineRequest
from jarvis.health.probes import (
    get_system_telemetry,
    scan_directory_for_secrets,
    scan_file_for_secrets,
)
from jarvis.health.quarantine import QuarantineManager, calculate_file_sha256
from jarvis.health.service import HealthService
from jarvis.health.stores import MemoryHealthStore

TEST_OWNER = uuid4()


def test_system_telemetry_probe():
    telemetry = get_system_telemetry()
    assert "cpu_percent" in telemetry
    assert "memory_percent" in telemetry
    assert "disk_free_gb" in telemetry
    assert telemetry["cpu_percent"] >= 0.0
    assert telemetry["disk_free_gb"] > 0.0


def test_secret_scanner():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        clean_file = tmp_path / "clean.py"
        clean_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

        secret_file = tmp_path / "secret.env"
        secret_file.write_text(
            "API_KEY='mock_secret_token_1234567890abcdef'\nAWS_KEY=AKIAIOSFODNN7EXAMPLE\n",
            encoding="utf-8",
        )

        clean_findings = scan_file_for_secrets(clean_file)
        assert len(clean_findings) == 0

        secret_findings = scan_file_for_secrets(secret_file)
        assert len(secret_findings) >= 2
        types = {f.secret_type for f in secret_findings}
        assert "aws_access_key" in types
        assert "api_secret_key" in types
        # Verify secrets are masked
        for f in secret_findings:
            assert "AKIAIOSFODNN7EXAMPLE" not in f.snippet_masked
            assert "*" in f.snippet_masked

        dir_findings = scan_directory_for_secrets(tmp_path)
        assert len(dir_findings) >= 2


def test_quarantine_isolation_and_restoration():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        source_file = tmp_path / "compromised_script.py"
        original_content = "import os; print('malicious payload')\n"
        source_file.write_text(original_content, encoding="utf-8")

        quarantine_dir = tmp_path / ".quarantine"
        manager = QuarantineManager(quarantine_dir)

        # 1. Quarantine file
        record = manager.quarantine_file(
            owner_id=TEST_OWNER,
            file_path=source_file,
            reason="Detected suspicious outbound beaconing",
        )
        assert not source_file.exists()
        assert Path(record.quarantine_path).exists()
        assert record.quarantine_path.endswith(".quarantine.bak")
        assert len(record.hash_sha256) == 64

        # 2. Restore file
        restored = manager.restore_file(record)
        assert source_file.exists()
        assert not Path(record.quarantine_path).exists()
        assert source_file.read_text(encoding="utf-8") == original_content
        assert restored.restored_at is not None


def test_quarantine_tamper_detection():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        source_file = tmp_path / "tamper_test.py"
        source_file.write_text("original content", encoding="utf-8")

        quarantine_dir = tmp_path / ".quarantine"
        manager = QuarantineManager(quarantine_dir)

        record = manager.quarantine_file(
            owner_id=TEST_OWNER,
            file_path=source_file,
            reason="Testing tamper check",
        )

        # Tamper with the quarantined file
        q_path = Path(record.quarantine_path)
        q_path.write_text("altered content", encoding="utf-8")

        with pytest.raises(ValueError, match="Cryptographic hash mismatch"):
            manager.restore_file(record)


def test_health_service_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        store = MemoryHealthStore()
        service = HealthService(store, quarantine_dir=tmp_path / ".quarantine", repo_root=tmp_path)

        # 1. Capture snapshot
        snap = service.capture_snapshot(TEST_OWNER)
        assert snap.owner_id == TEST_OWNER
        assert snap.disk_free_gb > 0.0

        # List snapshots
        page = service.list_snapshots(TEST_OWNER)
        assert page.total == 1
        assert page.items[0].id == snap.id

        # 2. Run diagnostics
        report = service.run_diagnostics(TEST_OWNER, scan_root=tmp_path)
        assert report.telemetry.id is not None
        assert len(report.recommendations) > 0

        # 3. Quarantine and restore via service
        test_file = tmp_path / "suspect.py"
        test_file.write_text("eval(malicious_code)", encoding="utf-8")

        q_record = service.quarantine_file(
            TEST_OWNER,
            QuarantineRequest(file_path=str(test_file), reason="Suspicious eval invocation"),
        )
        assert not test_file.exists()

        # List quarantined files
        q_page = service.list_quarantined_files(TEST_OWNER)
        assert q_page.total == 1
        assert q_page.items[0].id == q_record.id

        # Restore file
        restored = service.restore_quarantined_file(TEST_OWNER, q_record.id)
        assert test_file.exists()
        assert restored.restored_at is not None
