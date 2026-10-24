-- Purpose: integration enrollments and their audit and approval categories (v4.8).
-- An enrollment is the owner's connection to one adapter. It moves pending -> active (only with an approved
-- request) -> revoked (final). Credentials are never stored: secret_ref names an environment variable that is
-- resolved only when an action runs. Only a mock adapter exists.
CREATE TABLE jarvis_app.integration_enrollments (
    id text PRIMARY KEY,
    owner_id uuid NOT NULL,
    adapter_id text NOT NULL,
    display_name text NOT NULL,
    secret_ref text,
    state text NOT NULL,
    created_at timestamptz NOT NULL,
    activated_at timestamptz,
    revoked_at timestamptz,
    last_action_at timestamptz,
    CONSTRAINT integration_enrollments_id_format CHECK (id ~ '^EN-[a-f0-9]{16}$'),
    CONSTRAINT integration_enrollments_adapter_length CHECK (length(adapter_id) BETWEEN 1 AND 80),
    CONSTRAINT integration_enrollments_name_length CHECK (length(display_name) BETWEEN 1 AND 100),
    CONSTRAINT integration_enrollments_secret_ref_format
        CHECK (secret_ref IS NULL OR secret_ref ~ '^env:JARVIS_INTEGRATION_[A-Z0-9_]{1,64}$'),
    CONSTRAINT integration_enrollments_state_allowed CHECK (state IN ('pending', 'active', 'revoked')),
    CONSTRAINT integration_enrollments_state_times
        CHECK ((state <> 'active' OR activated_at IS NOT NULL) AND (state <> 'revoked' OR revoked_at IS NOT NULL))
);
CREATE INDEX integration_enrollments_owner_created ON jarvis_app.integration_enrollments (owner_id, created_at DESC);

-- Keep every earlier audit category (0006, 0058, 0059, 0060) and add 'integration'.
ALTER TABLE jarvis_app.audit_events DROP CONSTRAINT audit_events_category_allowed;
ALTER TABLE jarvis_app.audit_events ADD CONSTRAINT audit_events_category_allowed
    CHECK (category IN ('tool', 'browser', 'approval', 'intelligence', 'orchestration', 'assistant', 'workflow',
                        'knowledge', 'research', 'scheduler', 'health', 'finance', 'improvement', 'integration'));

-- Keep every earlier approval category (0006, 0062, 0063) and add the enforced 'integration' category.
ALTER TABLE jarvis_app.approval_requests DROP CONSTRAINT approval_requests_category_allowed;
ALTER TABLE jarvis_app.approval_requests ADD CONSTRAINT approval_requests_category_allowed
    CHECK (category IN ('tool', 'browser', 'autonomous', 'improvement', 'quarantine', 'workflow', 'integration'));
