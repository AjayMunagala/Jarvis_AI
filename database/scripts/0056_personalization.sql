-- Purpose: owner-scoped personalization settings, preferences and preference proposals (v5.6).
-- Numbered 0056 (v5.6) so it cannot collide with migrations added by the parallel v3.1-v4.0 workstream.
-- Only allowlisted presentation and routing preferences are representable; no security settings live here.
CREATE TABLE jarvis_app.personalization_settings (
    owner_id uuid PRIMARY KEY,
    enabled boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL
);

CREATE TABLE jarvis_app.personalization_preferences (
    owner_id uuid NOT NULL,
    key text NOT NULL,
    scope text NOT NULL,
    value jsonb NOT NULL,
    provenance text NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (owner_id, key, scope),
    CONSTRAINT personalization_key_allowed CHECK (key IN ('response_detail', 'output_format', 'language',
        'coding_explanation', 'preferred_providers', 'latency_quality', 'privacy_routing')),
    CONSTRAINT personalization_scope_format CHECK (scope ~ '^[a-z_]{1,40}$'),
    CONSTRAINT personalization_provenance_allowed CHECK (provenance IN ('explicit', 'inferred_confirmed'))
);

CREATE TABLE jarvis_app.personalization_proposals (
    id text PRIMARY KEY,
    owner_id uuid NOT NULL,
    key text NOT NULL,
    scope text NOT NULL,
    value jsonb NOT NULL,
    confidence double precision NOT NULL,
    evidence_count integer NOT NULL,
    basis text NOT NULL,
    status text NOT NULL DEFAULT 'proposed',
    created_at timestamptz NOT NULL,
    decided_at timestamptz,
    CONSTRAINT personalization_proposals_id_format CHECK (id ~ '^PP-[a-f0-9]{16}$'),
    CONSTRAINT personalization_proposals_key_allowed CHECK (key IN ('response_detail', 'output_format', 'language',
        'coding_explanation', 'preferred_providers', 'latency_quality', 'privacy_routing')),
    CONSTRAINT personalization_proposals_confidence CHECK (confidence >= 0 AND confidence < 1),
    CONSTRAINT personalization_proposals_status CHECK (status IN ('proposed', 'accepted', 'rejected')),
    CONSTRAINT personalization_proposals_decision CHECK ((status = 'proposed') = (decided_at IS NULL))
);

CREATE INDEX personalization_proposals_owner_idx ON jarvis_app.personalization_proposals (owner_id, created_at DESC);
