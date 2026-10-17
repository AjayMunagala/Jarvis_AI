-- Purpose: durable, owner-scoped audit events and approval requests (v2.9).
-- Records contain metadata only (no tool arguments, page content, or secrets).
-- Approvals are recorded only: no column or state represents execution.
CREATE TABLE jarvis_app.audit_events (
    id text PRIMARY KEY,
    owner_id uuid NOT NULL,
    occurred_at timestamptz NOT NULL,
    category text NOT NULL,
    event_type text NOT NULL,
    decision text NOT NULL,
    status text NOT NULL,
    subject text NOT NULL,
    correlation_id text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT audit_events_id_format CHECK (id ~ '^AE-[a-f0-9]{16}$'),
    CONSTRAINT audit_events_category_allowed CHECK (category IN ('tool', 'browser', 'approval')),
    CONSTRAINT audit_events_subject_length CHECK (length(subject) <= 200),
    CONSTRAINT audit_events_details_object CHECK (jsonb_typeof(details) = 'object')
);

CREATE INDEX audit_events_owner_recent_idx
    ON jarvis_app.audit_events (owner_id, occurred_at DESC, id DESC);

CREATE TABLE jarvis_app.approval_requests (
    id text PRIMARY KEY,
    owner_id uuid NOT NULL,
    category text NOT NULL,
    action text NOT NULL,
    target text NOT NULL,
    reason text NOT NULL,
    risk text NOT NULL,
    expected_side_effects text NOT NULL,
    scope_sha256 char(64) NOT NULL,
    correlation_id text,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    state text NOT NULL DEFAULT 'pending',
    decided_at timestamptz,
    decision_note text,
    CONSTRAINT approval_requests_id_format CHECK (id ~ '^AR-[a-f0-9]{16}$'),
    CONSTRAINT approval_requests_category_allowed CHECK (category IN ('tool', 'browser')),
    CONSTRAINT approval_requests_state_allowed CHECK (state IN ('pending', 'approved', 'rejected', 'expired')),
    CONSTRAINT approval_requests_risk_allowed CHECK (risk IN ('low', 'medium', 'high', 'critical', 'consequential')),
    CONSTRAINT approval_requests_scope_format CHECK (scope_sha256 ~ '^[a-f0-9]{64}$'),
    CONSTRAINT approval_requests_expiry_after_creation CHECK (expires_at > created_at),
    CONSTRAINT approval_requests_decision_consistent CHECK ((state IN ('approved', 'rejected')) = (decided_at IS NOT NULL))
);

CREATE INDEX approval_requests_owner_recent_idx
    ON jarvis_app.approval_requests (owner_id, created_at DESC, id DESC);
