-- Purpose: add single-owner isolation to long-term memory records.
-- Existing records intentionally remain unowned and are inaccessible through the API.
-- Do not add a default or backfill without a separately reviewed operator procedure.
ALTER TABLE jarvis_app.memories
    ADD COLUMN owner_id uuid;

CREATE INDEX memories_owner_listing_order_idx
    ON jarvis_app.memories (owner_id, created_at DESC, id ASC)
    WHERE owner_id IS NOT NULL;
