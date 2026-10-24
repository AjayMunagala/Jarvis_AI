-- Purpose: opt-in event sources (v7.3): RSS/Atom feeds and watched local folders, and the items each has seen.
-- The checks mirror the API validation. Seen items belong to a source of the same owner (composite foreign key)
-- and are removed with it (ON DELETE CASCADE). The application keeps at most 1000 seen items per source and at
-- most 50 sources per owner (checked in the same transaction as the insert). Audit events use the existing
-- 'scheduler' category, so the audit category constraint is unchanged.
-- Folder files are keyed by relative path; ``version`` is the size and mtime last observed and
-- ``settled_version`` the one already reported (a change is reported once it is unchanged across two polls).
CREATE TABLE jarvis_app.event_sources (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    url text,
    folder_path text,
    recursive_depth integer NOT NULL DEFAULT 0,
    enabled boolean NOT NULL DEFAULT true,
    poll_interval_minutes integer NOT NULL DEFAULT 60,
    importance text NOT NULL DEFAULT 'normal',
    include_keywords text[] NOT NULL DEFAULT '{}',
    exclude_keywords text[] NOT NULL DEFAULT '{}',
    max_items_per_poll integer NOT NULL DEFAULT 5,
    last_polled_at timestamptz,
    last_success_at timestamptz,
    consecutive_failures integer NOT NULL DEFAULT 0,
    last_error text,
    backoff_until timestamptz,
    last_notified_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT event_sources_id_owner UNIQUE (id, owner_id),
    CONSTRAINT event_sources_kind_config CHECK (
        (kind = 'rss' AND url IS NOT NULL AND char_length(url) BETWEEN 1 AND 2000 AND folder_path IS NULL
         AND recursive_depth = 0)
        OR
        (kind = 'folder' AND folder_path IS NOT NULL AND char_length(folder_path) BETWEEN 1 AND 1000 AND url IS NULL)),
    CONSTRAINT event_sources_name_length CHECK (char_length(name) BETWEEN 1 AND 120),
    CONSTRAINT event_sources_recursive_depth_range CHECK (recursive_depth BETWEEN 0 AND 3),
    CONSTRAINT event_sources_poll_interval_range CHECK (poll_interval_minutes BETWEEN 15 AND 1440),
    CONSTRAINT event_sources_importance_allowed CHECK (importance IN ('low', 'normal', 'high')),
    CONSTRAINT event_sources_keywords_count CHECK (cardinality(include_keywords) <= 20
                                                   AND cardinality(exclude_keywords) <= 20
                                                   AND array_position(include_keywords, NULL) IS NULL
                                                   AND array_position(exclude_keywords, NULL) IS NULL),
    CONSTRAINT event_sources_max_items_range CHECK (max_items_per_poll BETWEEN 1 AND 20),
    CONSTRAINT event_sources_failures_nonnegative CHECK (consecutive_failures >= 0),
    CONSTRAINT event_sources_last_error_length CHECK (last_error IS NULL OR char_length(last_error) BETWEEN 1 AND 200)
);
CREATE INDEX event_sources_owner_created ON jarvis_app.event_sources (owner_id, created_at DESC);

CREATE TABLE jarvis_app.event_source_items (
    source_id uuid NOT NULL,
    owner_id uuid NOT NULL,
    item_key text NOT NULL,
    title text NOT NULL,
    link text,
    published_at timestamptz,
    summary text,
    matched boolean NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    version text,
    settled_version text,
    notified_at timestamptz,
    PRIMARY KEY (source_id, item_key),
    CONSTRAINT event_source_items_source FOREIGN KEY (source_id, owner_id)
        REFERENCES jarvis_app.event_sources (id, owner_id) ON DELETE CASCADE,
    CONSTRAINT event_source_items_key_format CHECK (item_key ~ '^[0-9a-f]{64}$'),
    CONSTRAINT event_source_items_title_length CHECK (char_length(title) BETWEEN 1 AND 300),
    CONSTRAINT event_source_items_link_length CHECK (link IS NULL OR char_length(link) BETWEEN 1 AND 2000),
    CONSTRAINT event_source_items_summary_length CHECK (summary IS NULL OR char_length(summary) BETWEEN 1 AND 1000),
    CONSTRAINT event_source_items_version_length CHECK ((version IS NULL OR char_length(version) BETWEEN 1 AND 100)
                                                        AND (settled_version IS NULL
                                                             OR char_length(settled_version) BETWEEN 1 AND 100))
);
CREATE INDEX event_source_items_source_seen ON jarvis_app.event_source_items (source_id, last_seen_at DESC);
CREATE INDEX event_source_items_owner_source_first ON jarvis_app.event_source_items (owner_id, source_id, first_seen_at DESC);
