-- Purpose: add explicitly managed long-term memories, separate from conversation history.
-- Prerequisites: migration runner access to create schema and tables.
-- No chat message or provider output is copied into this table automatically.
CREATE TABLE jarvis_app.memories (
    id uuid PRIMARY KEY,
    content text NOT NULL,
    category text,
    source_reference text,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT memories_content_nonblank CHECK (length(btrim(content)) > 0),
    CONSTRAINT memories_content_max_length CHECK (length(content) <= 4000),
    CONSTRAINT memories_category_valid CHECK (
        category IS NULL OR (length(btrim(category)) BETWEEN 1 AND 80)
    ),
    CONSTRAINT memories_source_reference_valid CHECK (
        source_reference IS NULL OR (length(btrim(source_reference)) BETWEEN 1 AND 512)
    )
);

CREATE INDEX memories_listing_order_idx
    ON jarvis_app.memories (created_at DESC, id ASC);

CREATE INDEX memories_content_search_idx
    ON jarvis_app.memories USING gin (to_tsvector('simple', content));
