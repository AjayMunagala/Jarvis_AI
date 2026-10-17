-- Purpose: durable, owner-scoped personal assistant tables (v3.1).
-- Includes tasks, personal notes, reminders, and local calendar events.
-- All records are strictly owner-isolated with defensive check constraints.

CREATE TABLE jarvis_app.tasks (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    title text NOT NULL,
    description text,
    status text NOT NULL DEFAULT 'pending',
    priority text NOT NULL DEFAULT 'medium',
    due_date timestamptz,
    tags text[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT tasks_title_length CHECK (length(title) >= 1 AND length(title) <= 200),
    CONSTRAINT tasks_description_length CHECK (description IS NULL OR length(description) <= 2000),
    CONSTRAINT tasks_status_allowed CHECK (status IN ('pending', 'in_progress', 'completed', 'cancelled')),
    CONSTRAINT tasks_priority_allowed CHECK (priority IN ('low', 'medium', 'high', 'urgent'))
);

CREATE INDEX tasks_owner_status_due_idx
    ON jarvis_app.tasks (owner_id, status, due_date ASC);

CREATE INDEX tasks_owner_updated_idx
    ON jarvis_app.tasks (owner_id, updated_at DESC);

CREATE TABLE jarvis_app.notes (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    title text NOT NULL,
    content text NOT NULL,
    tags text[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT notes_title_length CHECK (length(title) >= 1 AND length(title) <= 200),
    CONSTRAINT notes_content_length CHECK (length(content) >= 1 AND length(content) <= 50000)
);

CREATE INDEX notes_owner_updated_idx
    ON jarvis_app.notes (owner_id, updated_at DESC);

CREATE TABLE jarvis_app.reminders (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    task_id uuid REFERENCES jarvis_app.tasks(id) ON DELETE SET NULL,
    title text NOT NULL,
    remind_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT reminders_title_length CHECK (length(title) >= 1 AND length(title) <= 200),
    CONSTRAINT reminders_status_allowed CHECK (status IN ('pending', 'triggered', 'acknowledged', 'dismissed'))
);

CREATE INDEX reminders_owner_schedule_idx
    ON jarvis_app.reminders (owner_id, status, remind_at ASC);

CREATE TABLE jarvis_app.calendar_events (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    title text NOT NULL,
    description text,
    start_time timestamptz NOT NULL,
    end_time timestamptz NOT NULL,
    location text,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT calendar_events_title_length CHECK (length(title) >= 1 AND length(title) <= 200),
    CONSTRAINT calendar_events_location_length CHECK (location IS NULL OR length(location) <= 300),
    CONSTRAINT calendar_events_time_order CHECK (end_time >= start_time)
);

CREATE INDEX calendar_events_owner_range_idx
    ON jarvis_app.calendar_events (owner_id, start_time ASC, end_time ASC);
