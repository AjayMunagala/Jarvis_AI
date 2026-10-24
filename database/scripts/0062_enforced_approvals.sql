-- Purpose: enforced, single-use approvals (v4.3).
-- Autonomous write commands, controlled-improvement apply/restore and file quarantine/restore now wait for an
-- approved request in the central gateway. An approved request is used at most once: consumed_at records the
-- moment the approved action was run, and can only be set on an approved request after its decision.
-- Tool and browser approvals stay record-only. Existing rows are unchanged (consumed_at is NULL).
ALTER TABLE jarvis_app.approval_requests DROP CONSTRAINT approval_requests_category_allowed;
ALTER TABLE jarvis_app.approval_requests ADD CONSTRAINT approval_requests_category_allowed
    CHECK (category IN ('tool', 'browser', 'autonomous', 'improvement', 'quarantine'));
ALTER TABLE jarvis_app.approval_requests ADD COLUMN consumed_at timestamptz;
ALTER TABLE jarvis_app.approval_requests ADD CONSTRAINT approval_requests_consumed_only_after_approval
    CHECK (consumed_at IS NULL OR (state = 'approved' AND decided_at IS NOT NULL AND consumed_at >= decided_at));
