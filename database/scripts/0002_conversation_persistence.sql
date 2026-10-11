-- Purpose: add optional, single-request conversation and message persistence.
-- Prerequisites: migration runner access to create schema and tables.
-- User and assistant messages are stored only when application persistence is enabled.
CREATE SCHEMA IF NOT EXISTS jarvis_app;

CREATE TABLE jarvis_app.conversations (
    id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE jarvis_app.messages (
    id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES jarvis_app.conversations(id) ON DELETE CASCADE,
    sequence_number integer NOT NULL CHECK (sequence_number >= 0),
    role text NOT NULL,
    content text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT messages_role_allowed CHECK (role IN ('user', 'assistant')),
    CONSTRAINT messages_content_nonblank CHECK (length(btrim(content)) > 0),
    CONSTRAINT messages_content_max_length CHECK (length(content) <= 40000)
);

CREATE UNIQUE INDEX messages_conversation_order_idx
    ON jarvis_app.messages (conversation_id, sequence_number);
