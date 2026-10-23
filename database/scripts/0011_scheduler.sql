-- 0011_scheduler.sql
-- v3.5 Proactive Assistance & Scheduling
-- Stores scheduled jobs and dispatched proactive notifications.

CREATE TABLE IF NOT EXISTS jarvis_app.scheduled_jobs (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL,
    name VARCHAR(200) NOT NULL,
    schedule_type VARCHAR(20) NOT NULL,
    schedule_expr VARCHAR(100) NOT NULL,
    action_type VARCHAR(50) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT true,
    next_run_at TIMESTAMPTZ,
    last_run_at TIMESTAMPTZ,
    last_status VARCHAR(20) NOT NULL DEFAULT 'idle',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_owner_next ON jarvis_app.scheduled_jobs(owner_id, enabled, next_run_at);

CREATE TABLE IF NOT EXISTS jarvis_app.notifications (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL,
    category VARCHAR(50) NOT NULL,
    title VARCHAR(300) NOT NULL,
    message TEXT NOT NULL,
    priority VARCHAR(20) NOT NULL DEFAULT 'normal',
    read BOOLEAN NOT NULL DEFAULT false,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_notifications_owner_read ON jarvis_app.notifications(owner_id, read, created_at DESC);
