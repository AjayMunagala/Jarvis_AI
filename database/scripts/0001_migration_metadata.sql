-- Purpose: create the versioned migration ledger used by the database setup runner.
-- Prerequisites: connection to the configured target database with CREATE privilege.
-- This foundation migration intentionally creates no application or user data tables.
CREATE SCHEMA IF NOT EXISTS jarvis_meta;

CREATE TABLE IF NOT EXISTS jarvis_meta.schema_migrations (
    migration_id text PRIMARY KEY,
    checksum_sha256 char(64) NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT schema_migrations_id_format CHECK (migration_id ~ '^[0-9]{4,}_[a-z0-9_]+$'),
    CONSTRAINT schema_migrations_checksum_format CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$')
);
