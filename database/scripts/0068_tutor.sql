-- Purpose: tutor (v6.5): learning goals, exercises with Leitner review state, and graded attempts.
-- The checks mirror the API validation. Exercises and attempts belong to a goal of the same owner (composite
-- foreign keys) and are removed with it (ON DELETE CASCADE); attempts are also removed with their exercise.
-- Each subtopic's length (1..120) and case-insensitive uniqueness are validated by the application.
CREATE TABLE jarvis_app.tutor_goals (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    topic text NOT NULL,
    level text NOT NULL,
    weekly_minutes integer NOT NULL,
    target_date date,
    subtopics text[] NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT tutor_goals_id_owner UNIQUE (id, owner_id),
    CONSTRAINT tutor_goals_topic_length CHECK (char_length(topic) BETWEEN 1 AND 200),
    CONSTRAINT tutor_goals_level_allowed CHECK (level IN ('beginner', 'intermediate', 'advanced')),
    CONSTRAINT tutor_goals_weekly_minutes_range CHECK (weekly_minutes BETWEEN 15 AND 1200),
    CONSTRAINT tutor_goals_subtopics_count CHECK (cardinality(subtopics) BETWEEN 1 AND 30
                                                  AND array_position(subtopics, NULL) IS NULL),
    CONSTRAINT tutor_goals_status_allowed CHECK (status IN ('active', 'completed', 'archived'))
);
CREATE INDEX tutor_goals_owner_created ON jarvis_app.tutor_goals (owner_id, created_at DESC);

CREATE TABLE jarvis_app.tutor_exercises (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    subtopic text NOT NULL,
    prompt text NOT NULL,
    kind text NOT NULL,
    choices text[],
    answer_index integer,
    accepted_answers text[],
    explanation text,
    source text NOT NULL,
    box integer NOT NULL DEFAULT 1,
    next_review_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    CONSTRAINT tutor_exercises_id_owner UNIQUE (id, owner_id),
    CONSTRAINT tutor_exercises_goal FOREIGN KEY (goal_id, owner_id)
        REFERENCES jarvis_app.tutor_goals (id, owner_id) ON DELETE CASCADE,
    CONSTRAINT tutor_exercises_subtopic_length CHECK (char_length(subtopic) BETWEEN 1 AND 120),
    CONSTRAINT tutor_exercises_prompt_length CHECK (char_length(prompt) BETWEEN 1 AND 2000),
    CONSTRAINT tutor_exercises_explanation_length CHECK (explanation IS NULL OR char_length(explanation) BETWEEN 1 AND 2000),
    CONSTRAINT tutor_exercises_source_allowed CHECK (source IN ('owner', 'model')),
    CONSTRAINT tutor_exercises_box_range CHECK (box BETWEEN 1 AND 5),
    CONSTRAINT tutor_exercises_answer_matches_kind CHECK (
        (kind = 'multiple_choice'
         AND choices IS NOT NULL AND cardinality(choices) BETWEEN 2 AND 6 AND array_position(choices, NULL) IS NULL
         AND answer_index IS NOT NULL AND answer_index >= 0 AND answer_index < cardinality(choices)
         AND accepted_answers IS NULL)
        OR
        (kind = 'short_answer'
         AND choices IS NULL AND answer_index IS NULL
         AND accepted_answers IS NOT NULL AND cardinality(accepted_answers) BETWEEN 1 AND 10
         AND array_position(accepted_answers, NULL) IS NULL))
);
CREATE INDEX tutor_exercises_owner_goal_review ON jarvis_app.tutor_exercises (owner_id, goal_id, next_review_at);

CREATE TABLE jarvis_app.tutor_attempts (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    exercise_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    given_answer text NOT NULL,
    correct boolean NOT NULL,
    created_at timestamptz NOT NULL,
    CONSTRAINT tutor_attempts_exercise FOREIGN KEY (exercise_id, owner_id)
        REFERENCES jarvis_app.tutor_exercises (id, owner_id) ON DELETE CASCADE,
    CONSTRAINT tutor_attempts_goal FOREIGN KEY (goal_id, owner_id)
        REFERENCES jarvis_app.tutor_goals (id, owner_id) ON DELETE CASCADE,
    CONSTRAINT tutor_attempts_given_answer_length CHECK (char_length(given_answer) BETWEEN 1 AND 500)
);
CREATE INDEX tutor_attempts_owner_goal_created ON jarvis_app.tutor_attempts (owner_id, goal_id, created_at DESC);
CREATE INDEX tutor_attempts_exercise ON jarvis_app.tutor_attempts (exercise_id);

-- Keep every earlier audit category (0006, 0058, 0059, 0060, 0065) and add 'tutor'.
ALTER TABLE jarvis_app.audit_events DROP CONSTRAINT audit_events_category_allowed;
ALTER TABLE jarvis_app.audit_events ADD CONSTRAINT audit_events_category_allowed
    CHECK (category IN ('tool', 'browser', 'approval', 'intelligence', 'orchestration', 'assistant', 'workflow',
                        'knowledge', 'research', 'scheduler', 'health', 'finance', 'improvement', 'integration',
                        'tutor'));
