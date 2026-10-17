-- Purpose: durable, owner-scoped memory update proposals and outdated-memory decisions (v5.3 hardening).
-- Forward-only and additive: no existing table (including jarvis_app.memories) is altered. target_ref is a
-- reference string, deliberately without a foreign key, because the memory schema belongs to the memory workstream.
-- An accepted mark_outdated row is the durable outdated state; status 'applying' marks an add whose write is
-- in progress or was interrupted and is never reported as accepted.
CREATE TABLE jarvis_app.memory_update_proposals (
    id text PRIMARY KEY,
    owner_id uuid NOT NULL,
    kind text NOT NULL,
    category text NOT NULL,
    content text,
    target_ref text,
    fact jsonb,
    source_ref text NOT NULL,
    reliability text NOT NULL,
    status text NOT NULL DEFAULT 'proposed',
    reason text NOT NULL,
    conflicts_with jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL,
    decided_at timestamptz,
    applied_ref text,
    last_error text,
    CONSTRAINT memory_update_proposals_id_format CHECK (id ~ '^MP-[a-f0-9]{16}$'),
    CONSTRAINT memory_update_proposals_kind_allowed CHECK (kind IN ('add', 'mark_outdated')),
    CONSTRAINT memory_update_proposals_category_allowed CHECK (category IN ('conversation', 'task', 'long_term',
        'project', 'procedural', 'retrieved_knowledge')),
    CONSTRAINT memory_update_proposals_reliability_allowed CHECK (reliability IN ('verified', 'user_stated',
        'document', 'inferred')),
    CONSTRAINT memory_update_proposals_status_allowed CHECK (status IN ('proposed', 'applying', 'accepted', 'rejected')),
    CONSTRAINT memory_update_proposals_kind_payload CHECK (
        (kind = 'add' AND content IS NOT NULL AND length(content) BETWEEN 1 AND 4000)
        OR (kind = 'mark_outdated' AND target_ref IS NOT NULL AND length(target_ref) BETWEEN 1 AND 300)),
    CONSTRAINT memory_update_proposals_decision_consistent CHECK (
        (status IN ('accepted', 'rejected')) = (decided_at IS NOT NULL)),
    CONSTRAINT memory_update_proposals_conflicts_array CHECK (jsonb_typeof(conflicts_with) = 'array')
);

CREATE INDEX memory_update_proposals_owner_status_idx
    ON jarvis_app.memory_update_proposals (owner_id, status, created_at DESC);
CREATE INDEX memory_update_proposals_outdated_idx
    ON jarvis_app.memory_update_proposals (owner_id, target_ref)
    WHERE kind = 'mark_outdated' AND status = 'accepted';
