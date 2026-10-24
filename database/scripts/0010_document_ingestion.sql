-- 0010_document_ingestion.sql
-- v3.4 Research & Document Intelligence
-- Stores extracted document text and multi-source research syntheses.

CREATE TABLE IF NOT EXISTS jarvis_app.parsed_documents (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL,
    filename VARCHAR(300) NOT NULL,
    content_type VARCHAR(100) NOT NULL,
    file_size_bytes BIGINT NOT NULL,
    page_count INT NOT NULL DEFAULT 1,
    extracted_text TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_parsed_docs_owner ON jarvis_app.parsed_documents(owner_id);

CREATE TABLE IF NOT EXISTS jarvis_app.research_syntheses (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL,
    topic VARCHAR(300) NOT NULL,
    source_urls JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_document_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    synthesis TEXT NOT NULL,
    citations JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_research_syntheses_owner ON jarvis_app.research_syntheses(owner_id);
