-- Purpose: allow the 'intelligence' audit category (v5.x hardening) without editing applied migration 0006.
-- Replaces the category check on jarvis_app.audit_events with the union of the 0006 categories and
-- 'intelligence'. Existing rows already satisfy the new, wider check. Any later migration that changes this
-- check (for example one adding categories for another workstream) must keep every category listed here.
ALTER TABLE jarvis_app.audit_events DROP CONSTRAINT audit_events_category_allowed;
ALTER TABLE jarvis_app.audit_events ADD CONSTRAINT audit_events_category_allowed
    CHECK (category IN ('tool', 'browser', 'approval', 'intelligence'));
