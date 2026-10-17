-- Purpose: distinguish ingested document knowledge from explicitly saved memories.
-- Existing rows remain 'memory': earlier imports cannot be identified reliably,
-- so no backfill is performed. Re-import a document to store it as a document.
ALTER TABLE jarvis_app.memories
    ADD COLUMN source_kind text NOT NULL DEFAULT 'memory';

ALTER TABLE jarvis_app.memories
    ADD CONSTRAINT memories_source_kind_allowed CHECK (source_kind IN ('memory', 'document'));

-- Supports owner-scoped exact-duplicate detection for document imports.
CREATE INDEX memories_owner_document_digest_idx
    ON jarvis_app.memories (owner_id, md5(content))
    WHERE source_kind = 'document' AND owner_id IS NOT NULL;
