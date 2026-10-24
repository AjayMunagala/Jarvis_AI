-- Purpose: travel and decision planning (v7.4): decisions, trips, itinerary segments and departure-reminder links.
-- No booking, payment or account data is stored; prices and times are owner-entered.
-- Decisions keep options and criteria as bounded JSONB arrays: they are always read, replaced and scored whole by
-- the application (at most 10 options x 10 criteria), never queried inside, so child tables would add joins and
-- per-row writes without a benefit. Their shape is validated by the API; the checks below bound type and size.
-- Segments and reminder links belong to a trip of the same owner (composite foreign keys) and are removed with it.
-- Reminder links point at assistant reminders by id without a foreign key: the assistant owns those rows, and the
-- application recreates a reminder the owner deleted there.
CREATE TABLE jarvis_app.travel_decisions (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    question text NOT NULL,
    options jsonb NOT NULL,
    criteria jsonb NOT NULL,
    notes text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT travel_decisions_question_length CHECK (char_length(question) BETWEEN 1 AND 500),
    CONSTRAINT travel_decisions_notes_length CHECK (notes IS NULL OR char_length(notes) BETWEEN 1 AND 2000),
    CONSTRAINT travel_decisions_options_shape CHECK (jsonb_typeof(options) = 'array'
                                                     AND jsonb_array_length(options) BETWEEN 2 AND 10
                                                     AND octet_length(options::text) <= 65536),
    CONSTRAINT travel_decisions_criteria_shape CHECK (jsonb_typeof(criteria) = 'array'
                                                      AND jsonb_array_length(criteria) BETWEEN 1 AND 10
                                                      AND octet_length(criteria::text) <= 16384)
);
CREATE INDEX travel_decisions_owner_created ON jarvis_app.travel_decisions (owner_id, created_at DESC);

CREATE TABLE jarvis_app.travel_trips (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    name text NOT NULL,
    travellers integer NOT NULL,
    currency text NOT NULL,
    budget numeric(15, 3),
    start_date date NOT NULL,
    end_date date NOT NULL,
    home_timezone text NOT NULL,
    daily_food numeric(15, 3),
    daily_local_transport numeric(15, 3),
    daily_other numeric(15, 3),
    min_connection_minutes_flight integer NOT NULL DEFAULT 60,
    min_connection_minutes_other integer NOT NULL DEFAULT 15,
    notes text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT travel_trips_id_owner UNIQUE (id, owner_id),
    CONSTRAINT travel_trips_name_length CHECK (char_length(name) BETWEEN 1 AND 200),
    CONSTRAINT travel_trips_travellers_range CHECK (travellers BETWEEN 1 AND 20),
    CONSTRAINT travel_trips_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT travel_trips_money_range CHECK (
        (budget IS NULL OR budget BETWEEN 0 AND 1000000000)
        AND (daily_food IS NULL OR daily_food BETWEEN 0 AND 1000000000)
        AND (daily_local_transport IS NULL OR daily_local_transport BETWEEN 0 AND 1000000000)
        AND (daily_other IS NULL OR daily_other BETWEEN 0 AND 1000000000)),
    CONSTRAINT travel_trips_dates CHECK (end_date >= start_date AND end_date - start_date < 366),
    CONSTRAINT travel_trips_timezone_length CHECK (char_length(home_timezone) BETWEEN 1 AND 64),
    CONSTRAINT travel_trips_connection_range CHECK (min_connection_minutes_flight BETWEEN 0 AND 1440
                                                    AND min_connection_minutes_other BETWEEN 0 AND 1440),
    CONSTRAINT travel_trips_notes_length CHECK (notes IS NULL OR char_length(notes) BETWEEN 1 AND 2000)
);
CREATE INDEX travel_trips_owner_start ON jarvis_app.travel_trips (owner_id, start_date DESC);

-- start_local/end_local are wall-clock times in start_timezone/end_timezone; start_utc/end_utc are the absolute
-- instants the application derived from them (used for ordering, overlap and reminders).
CREATE TABLE jarvis_app.travel_segments (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    trip_id uuid NOT NULL,
    kind text NOT NULL,
    title text NOT NULL,
    from_place text,
    to_place text,
    start_local timestamp NOT NULL,
    start_timezone text NOT NULL,
    end_local timestamp NOT NULL,
    end_timezone text NOT NULL,
    start_utc timestamptz NOT NULL,
    end_utc timestamptz NOT NULL,
    reference text,
    cost_amount numeric(15, 3),
    cost_currency text,
    notes text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT travel_segments_id_owner UNIQUE (id, owner_id),
    CONSTRAINT travel_segments_id_trip_owner UNIQUE (id, trip_id, owner_id),
    CONSTRAINT travel_segments_trip FOREIGN KEY (trip_id, owner_id)
        REFERENCES jarvis_app.travel_trips (id, owner_id) ON DELETE CASCADE,
    CONSTRAINT travel_segments_kind_allowed CHECK (kind IN ('flight', 'train', 'bus', 'car', 'ferry', 'stay',
                                                            'activity', 'other')),
    CONSTRAINT travel_segments_title_length CHECK (char_length(title) BETWEEN 1 AND 200),
    CONSTRAINT travel_segments_place_length CHECK ((from_place IS NULL OR char_length(from_place) BETWEEN 1 AND 200)
                                                   AND (to_place IS NULL OR char_length(to_place) BETWEEN 1 AND 200)),
    CONSTRAINT travel_segments_timezone_length CHECK (char_length(start_timezone) BETWEEN 1 AND 64
                                                      AND char_length(end_timezone) BETWEEN 1 AND 64),
    CONSTRAINT travel_segments_order CHECK (end_utc > start_utc),
    CONSTRAINT travel_segments_reference_length CHECK (reference IS NULL OR char_length(reference) BETWEEN 1 AND 100),
    CONSTRAINT travel_segments_cost CHECK (
        (cost_amount IS NULL AND cost_currency IS NULL)
        OR (cost_amount BETWEEN 0 AND 1000000000 AND cost_currency ~ '^[A-Z]{3}$')),
    CONSTRAINT travel_segments_notes_length CHECK (notes IS NULL OR char_length(notes) BETWEEN 1 AND 2000)
);
CREATE INDEX travel_segments_trip_start ON jarvis_app.travel_segments (owner_id, trip_id, start_utc);

CREATE TABLE jarvis_app.travel_reminder_links (
    segment_id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    trip_id uuid NOT NULL,
    reminder_id uuid NOT NULL,
    lead_minutes integer NOT NULL,
    remind_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT travel_reminder_links_segment FOREIGN KEY (segment_id, trip_id, owner_id)
        REFERENCES jarvis_app.travel_segments (id, trip_id, owner_id) ON DELETE CASCADE,
    CONSTRAINT travel_reminder_links_trip FOREIGN KEY (trip_id, owner_id)
        REFERENCES jarvis_app.travel_trips (id, owner_id) ON DELETE CASCADE,
    CONSTRAINT travel_reminder_links_lead_range CHECK (lead_minutes BETWEEN 15 AND 1440)
);
CREATE INDEX travel_reminder_links_owner_trip ON jarvis_app.travel_reminder_links (owner_id, trip_id);

-- Keep every earlier audit category (0006, 0058, 0059, 0060, 0065, 0068) and add 'travel'.
ALTER TABLE jarvis_app.audit_events DROP CONSTRAINT audit_events_category_allowed;
ALTER TABLE jarvis_app.audit_events ADD CONSTRAINT audit_events_category_allowed
    CHECK (category IN ('tool', 'browser', 'approval', 'intelligence', 'orchestration', 'assistant', 'workflow',
                        'knowledge', 'research', 'scheduler', 'health', 'finance', 'improvement', 'integration',
                        'tutor', 'travel'));
