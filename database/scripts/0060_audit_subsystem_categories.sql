-- Purpose: per-subsystem audit categories for the v3.x services (v4.2), without editing 0006, 0058 or 0059.
-- Before v4.2 every v3.x service audited as 'tool' (and self-improvement as 'approval'), which made the
-- activity trail impossible to filter by subsystem. This replaces the category check with the union of every
-- earlier category plus one category per v3.x subsystem. Existing rows already satisfy the wider check.
-- Any later migration that changes this check must keep every category listed here.
ALTER TABLE jarvis_app.audit_events DROP CONSTRAINT audit_events_category_allowed;
ALTER TABLE jarvis_app.audit_events ADD CONSTRAINT audit_events_category_allowed
    CHECK (category IN ('tool', 'browser', 'approval', 'intelligence', 'orchestration', 'assistant', 'workflow',
                        'knowledge', 'research', 'scheduler', 'health', 'finance', 'improvement'));
