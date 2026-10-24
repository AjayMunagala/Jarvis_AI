-- Purpose: model usage and cost tracking (v6.1).
-- One row per call to a real model provider: provider, model, primary or fallback, outcome, the token counts the
-- provider reported (NULL when none), duration and the request's correlation ID. Prompt and reply text are never
-- stored. Usage is tracked per installation.
CREATE TABLE jarvis_app.usage_events (
    id text PRIMARY KEY,
    occurred_at timestamptz NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    role text NOT NULL,
    outcome text NOT NULL,
    prompt_tokens integer,
    completion_tokens integer,
    duration_ms double precision NOT NULL,
    correlation_id text,
    CONSTRAINT usage_events_id_format CHECK (id ~ '^UE-[a-f0-9]{16}$'),
    CONSTRAINT usage_events_role_allowed CHECK (role IN ('primary', 'fallback')),
    CONSTRAINT usage_events_outcome_allowed CHECK (outcome IN ('succeeded', 'failed', 'timeout', 'cancelled')),
    CONSTRAINT usage_events_tokens_non_negative CHECK (coalesce(prompt_tokens, 0) >= 0 AND coalesce(completion_tokens, 0) >= 0),
    CONSTRAINT usage_events_duration_non_negative CHECK (duration_ms >= 0)
);
CREATE INDEX usage_events_occurred ON jarvis_app.usage_events (occurred_at);
