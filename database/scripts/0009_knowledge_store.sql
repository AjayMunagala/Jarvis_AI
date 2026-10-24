-- 0009_knowledge_store.sql
-- v3.3 Knowledge Intelligence & Context Continuity
-- Stores ingested knowledge documents, partitioned chunks, and vector embedding representations.

CREATE TABLE IF NOT EXISTS jarvis_app.knowledge_documents (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL,
    title VARCHAR(300) NOT NULL,
    source_type VARCHAR(50) NOT NULL DEFAULT 'manual',
    source_uri TEXT,
    content TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT knowledge_docs_source_check CHECK (source_type IN ('manual', 'note', 'file', 'web', 'memory'))
);

CREATE INDEX IF NOT EXISTS idx_knowledge_docs_owner ON jarvis_app.knowledge_documents(owner_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_docs_fts ON jarvis_app.knowledge_documents USING gin(to_tsvector('english', title || ' ' || content));

CREATE TABLE IF NOT EXISTS jarvis_app.knowledge_chunks (
    id UUID PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES jarvis_app.knowledge_documents(id) ON DELETE CASCADE,
    owner_id UUID NOT NULL,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,
    token_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_doc ON jarvis_app.knowledge_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_owner ON jarvis_app.knowledge_chunks(owner_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_fts ON jarvis_app.knowledge_chunks USING gin(to_tsvector('english', content));

CREATE TABLE IF NOT EXISTS jarvis_app.knowledge_embeddings (
    chunk_id UUID PRIMARY KEY REFERENCES jarvis_app.knowledge_chunks(id) ON DELETE CASCADE,
    owner_id UUID NOT NULL,
    model_name VARCHAR(100) NOT NULL,
    embedding JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_knowledge_embeddings_owner ON jarvis_app.knowledge_embeddings(owner_id);
