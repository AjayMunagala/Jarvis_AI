-- Purpose: mark knowledge chunks that came from untrusted documents (v4.6).
-- Research documents indexed in the knowledge store are untrusted content. The flag is kept on every chunk,
-- so a chunk keeps its marking after the document is split, wherever it is returned or used as context.
-- Existing chunks are owner-entered knowledge and default to false.
ALTER TABLE jarvis_app.knowledge_chunks ADD COLUMN untrusted boolean NOT NULL DEFAULT false;
