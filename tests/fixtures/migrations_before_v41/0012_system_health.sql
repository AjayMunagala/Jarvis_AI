-- 0012_system_health.sql
-- v3.6 System Health & Reliability
-- Stores system health telemetry snapshots and quarantined suspect files.

CREATE TABLE IF NOT EXISTS jarvis_app.health_snapshots (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL REFERENCES jarvis_app.users(id) ON DELETE CASCADE,
    cpu_percent NUMERIC(5, 2) NOT NULL,
    memory_percent NUMERIC(5, 2) NOT NULL,
    disk_free_gb NUMERIC(10, 2) NOT NULL,
    services_status JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_health_snapshots_owner ON jarvis_app.health_snapshots(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS jarvis_app.quarantined_files (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL REFERENCES jarvis_app.users(id) ON DELETE CASCADE,
    original_path VARCHAR(500) NOT NULL,
    quarantine_path VARCHAR(500) NOT NULL,
    reason VARCHAR(300) NOT NULL,
    hash_sha256 VARCHAR(64) NOT NULL,
    approved_by UUID REFERENCES jarvis_app.users(id),
    quarantined_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    restored_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_quarantined_files_owner ON jarvis_app.quarantined_files(owner_id, quarantined_at DESC);
