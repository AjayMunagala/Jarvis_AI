# System Health & Reliability (v3.6)

v3.6 introduces system telemetry snapshots, diagnostic runs with automated secret/credential scanning, and an approval-gated cryptographic quarantine mechanism.

## Architecture & Security Principles

### 1. Read-Only System Telemetry
- Captures system resource metrics: CPU percent, Memory percent, and Free Disk Space in GB.
- Verifies operational readiness across core components (`api_server`, `database`, `orchestrator`).
- Telemetry captures are non-invasive and execute without modifying host system files or states.

### 2. Secret Exposure Scanner
- Deeply inspects repository and workspace files (filtering `.git`, `.venv`, `node_modules`, `dist`, `build`).
- Identifies uncommitted credentials matching high-risk signatures:
  - AWS Access Key IDs (`AKIA...`)
  - Cryptographic private keys (`-----BEGIN ... PRIVATE KEY-----`)
  - API secret keys and bearer tokens (`api_key=...`, `client_secret=...`)
- Masks detected secrets in diagnostic reports (`AKIA******`) to prevent credential leakage in logs.

### 3. Approval-Gated Quarantine & Cryptographic Restoration
- Suspect or compromised files are never silently deleted.
- Quarantining moves the suspect file into `.quarantine/` with a non-executable extension (`.quarantine.bak`).
- Pre-quarantine SHA-256 cryptographic hashes are computed and stored.
- Restoration verifies that the quarantined backup hash matches exactly before allowing restoration to its original path, protecting against offline tampering.
- Requires explicit owner capability `SECURITY_QUARANTINE_FILE`.

---

## Database Migration (`0012_system_health.sql`)

- `jarvis_app.health_snapshots`: `id` (UUID PK), `owner_id` (UUID), `cpu_percent` (NUMERIC 5,2), `memory_percent` (NUMERIC 5,2), `disk_free_gb` (NUMERIC 10,2), `services_status` (JSONB), `created_at` (TIMESTAMPTZ).
  - Index: `idx_health_snapshots_owner` on `(owner_id, created_at DESC)`.
- `jarvis_app.quarantined_files`: `id` (UUID PK), `owner_id` (UUID), `original_path` (VARCHAR 500), `quarantine_path` (VARCHAR 500), `reason` (VARCHAR 300), `hash_sha256` (VARCHAR 64), `approved_by` (UUID), `quarantined_at` (TIMESTAMPTZ), `restored_at` (TIMESTAMPTZ).
  - Index: `idx_quarantined_files_owner` on `(owner_id, quarantined_at DESC)`.

---

## API Endpoints

All endpoints are mounted under `/health`:

| Endpoint | Method | Capability | Description |
|---|---|---|---|
| `/health/diagnostic/snapshots` | `GET` | `HEALTH_READ` | List telemetry snapshots with pagination |
| `/health/diagnostic/snapshots` | `POST` | `HEALTH_DIAGNOSTIC_RUN` | Capture on-demand telemetry snapshot |
| `/health/diagnostic/run` | `POST` | `HEALTH_DIAGNOSTIC_RUN` | Run full diagnostic suite with secret scanning |
| `/health/security/quarantined` | `GET` | `HEALTH_READ` | List quarantined files |
| `/health/security/quarantine` | `POST` | `SECURITY_QUARANTINE_FILE` | Quarantine suspect file with SHA-256 verification |
| `/health/security/quarantined/{id}/restore` | `POST` | `SECURITY_QUARANTINE_FILE` | Restore quarantined file after hash validation |

---

## Verification & Test Coverage

- `tests/test_health.py`: Unit tests for telemetry probes, secret pattern scanning and masking, quarantine file movement and extension renaming, SHA-256 tamper detection, and service lifecycle.
- `tests/test_health_api.py`: FastAPI TestClient integration tests verifying authentication, capability checks (401/403), telemetry snapshot capture, diagnostic reporting, and quarantine/restore endpoints.
