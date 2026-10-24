-- Purpose: allow "not measured" telemetry values (v4.2).
-- Before v4.2 the probe stored invented constants (CPU 15.0, memory 42.0, disk 50.0 on error). Values are now
-- measured from the operating system or stored as NULL when they cannot be measured; NULL means "unavailable",
-- never zero. Existing rows are unchanged.
ALTER TABLE jarvis_app.health_snapshots ALTER COLUMN cpu_percent DROP NOT NULL;
ALTER TABLE jarvis_app.health_snapshots ALTER COLUMN memory_percent DROP NOT NULL;
ALTER TABLE jarvis_app.health_snapshots ALTER COLUMN disk_free_gb DROP NOT NULL;
