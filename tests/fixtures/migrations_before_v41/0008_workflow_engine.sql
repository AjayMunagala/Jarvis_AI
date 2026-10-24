-- 0008_workflow_engine.sql
-- v3.2 Advanced Planning & Workflow Orchestration (DAG-based Workflows)
-- Note: Includes recovery_metadata jsonb in workflow_step_runs to satisfy v3.8 persistent recovery requirements without requiring a separate migration.

CREATE TABLE IF NOT EXISTS jarvis_app.workflow_definitions (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL REFERENCES jarvis_app.users(id) ON DELETE CASCADE,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    dag_spec JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT workflow_definitions_name_not_empty CHECK (length(trim(name)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_workflow_definitions_owner ON jarvis_app.workflow_definitions(owner_id);

CREATE TABLE IF NOT EXISTS jarvis_app.workflow_runs (
    id UUID PRIMARY KEY,
    workflow_id UUID REFERENCES jarvis_app.workflow_definitions(id) ON DELETE SET NULL,
    owner_id UUID NOT NULL REFERENCES jarvis_app.users(id) ON DELETE CASCADE,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    inputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    outputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT workflow_runs_status_check CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_owner_status ON jarvis_app.workflow_runs(owner_id, status);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow ON jarvis_app.workflow_runs(workflow_id);

CREATE TABLE IF NOT EXISTS jarvis_app.workflow_step_runs (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES jarvis_app.workflow_runs(id) ON DELETE CASCADE,
    step_id VARCHAR(100) NOT NULL,
    step_name VARCHAR(200) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    inputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    outputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    recovery_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT workflow_step_runs_status_check CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS idx_workflow_step_runs_run ON jarvis_app.workflow_step_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_workflow_step_runs_status ON jarvis_app.workflow_step_runs(status);
