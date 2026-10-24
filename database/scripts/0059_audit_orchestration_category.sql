-- Purpose: allow the 'orchestration' audit category (v4.0 capstone & v3.8 cross-module workflows)
-- without editing previously applied migrations 0006 or 0058.
-- Replaces the category check on jarvis_app.audit_events with the union of all earlier categories plus 'orchestration'.
-- Existing rows already satisfy the new, wider check.
ALTER TABLE jarvis_app.audit_events DROP CONSTRAINT audit_events_category_allowed;
ALTER TABLE jarvis_app.audit_events ADD CONSTRAINT audit_events_category_allowed
    CHECK (category IN ('tool', 'browser', 'approval', 'intelligence', 'orchestration'));
