-- Purpose: durable, owner-scoped controlled improvement (v4.9).
-- Before v4.9 proposals, restore snapshots and changes to virtual prompt targets lived in memory only and were
-- lost on restart, and snapshots were not owner-scoped. Each proposal now also stores the evaluation that was
-- run before it could be approved (baseline and candidate scores, regressions).
CREATE TABLE jarvis_app.improvement_proposals (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    target_name text NOT NULL,
    original_content text NOT NULL,
    proposed_content text NOT NULL,
    diff text NOT NULL,
    rationale text NOT NULL,
    status text NOT NULL,
    snapshot_id uuid,
    evaluation jsonb,
    created_at timestamptz NOT NULL,
    decided_at timestamptz,
    CONSTRAINT improvement_proposals_status_allowed CHECK (status IN ('pending', 'approved', 'rejected', 'restored')),
    CONSTRAINT improvement_proposals_target_length CHECK (length(target_name) BETWEEN 1 AND 200),
    CONSTRAINT improvement_proposals_rationale_length CHECK (length(rationale) BETWEEN 1 AND 1000)
);
CREATE INDEX improvement_proposals_owner_created ON jarvis_app.improvement_proposals (owner_id, created_at DESC);

CREATE TABLE jarvis_app.improvement_snapshots (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    target_name text NOT NULL,
    content text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE INDEX improvement_snapshots_owner ON jarvis_app.improvement_snapshots (owner_id);

-- Content of virtual prompt targets (the built-in prompts that have no file). Absent rows mean "built-in default".
CREATE TABLE jarvis_app.improvement_target_contents (
    target_name text PRIMARY KEY,
    content text NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT improvement_target_contents_name_length CHECK (length(target_name) BETWEEN 1 AND 200)
);
