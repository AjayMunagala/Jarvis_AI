-- Purpose: workflow runs with write steps need an approved request (v4.4).
-- Adds the enforced 'workflow' approval category; every earlier category stays allowed. Existing rows are unchanged.
ALTER TABLE jarvis_app.approval_requests DROP CONSTRAINT approval_requests_category_allowed;
ALTER TABLE jarvis_app.approval_requests ADD CONSTRAINT approval_requests_category_allowed
    CHECK (category IN ('tool', 'browser', 'autonomous', 'improvement', 'quarantine', 'workflow'));
