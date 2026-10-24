-- Purpose: timestamped forecast snapshots and their accuracy tracking (v7.2, analysis only).
-- A snapshot freezes what GET /finance/forecast said at created_at: settings, status, model version and a
-- fingerprint of the history it used (count, latest date and net total of the transactions dated before the
-- as_of month, plus the truncation flags). Its targets live in a child table, one row per series and horizon,
-- rather than in a JSONB column: accuracy filters completed months in SQL on (owner_id, target_month), and each
-- value gets a typed CHECK instead of a JSON size limit. Targets belong to a snapshot of the same owner
-- (composite foreign key) and are removed with it. Snapshots are immutable: the application has no update path
-- and a trigger refuses UPDATE on both tables. The per-owner cap (500, oldest deleted first) and the
-- one-per-UTC-day guard are enforced by the application under a per-owner advisory lock.

CREATE TABLE jarvis_app.forecast_snapshots (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    created_at timestamptz NOT NULL,
    as_of timestamptz NOT NULL,
    horizon integer NOT NULL,
    min_train integer NOT NULL,
    status text NOT NULL,
    model_version text NOT NULL,
    months_available integer NOT NULL,
    transactions_considered integer NOT NULL,
    history_transaction_count integer NOT NULL,
    history_latest_transaction_date timestamptz,
    history_net_total numeric(20, 2) NOT NULL,
    truncated boolean NOT NULL,
    accounts_truncated boolean NOT NULL,
    target_count integer NOT NULL,
    CONSTRAINT forecast_snapshots_id_owner UNIQUE (id, owner_id),
    CONSTRAINT forecast_snapshots_horizon CHECK (horizon BETWEEN 1 AND 12),
    CONSTRAINT forecast_snapshots_min_train CHECK (min_train BETWEEN 3 AND 24),
    CONSTRAINT forecast_snapshots_status CHECK (status IN ('ok', 'insufficient_history')),
    CONSTRAINT forecast_snapshots_model_version CHECK (char_length(model_version) BETWEEN 1 AND 32),
    CONSTRAINT forecast_snapshots_counts CHECK (
        months_available >= 0 AND transactions_considered >= 0 AND history_transaction_count >= 0
        AND target_count BETWEEN 0 AND 36),
    CONSTRAINT forecast_snapshots_targets_need_ok CHECK (status = 'ok' OR target_count = 0)
);
CREATE INDEX forecast_snapshots_owner_created ON jarvis_app.forecast_snapshots (owner_id, created_at DESC, id DESC);

CREATE TABLE jarvis_app.forecast_snapshot_targets (
    snapshot_id uuid NOT NULL,
    owner_id uuid NOT NULL,
    series text NOT NULL,
    target_month date NOT NULL,
    horizon integer NOT NULL,
    method text NOT NULL,
    point double precision NOT NULL,
    lower_bound double precision,
    upper_bound double precision,
    interval_basis text NOT NULL,
    PRIMARY KEY (snapshot_id, series, horizon),
    CONSTRAINT forecast_snapshot_targets_snapshot FOREIGN KEY (snapshot_id, owner_id)
        REFERENCES jarvis_app.forecast_snapshots (id, owner_id) ON DELETE CASCADE,
    CONSTRAINT forecast_snapshot_targets_series CHECK (series IN ('net', 'income', 'spending')),
    CONSTRAINT forecast_snapshot_targets_month CHECK (extract(day FROM target_month) = 1),
    CONSTRAINT forecast_snapshot_targets_horizon CHECK (horizon BETWEEN 1 AND 12),
    CONSTRAINT forecast_snapshot_targets_method CHECK (char_length(method) BETWEEN 1 AND 64),
    CONSTRAINT forecast_snapshot_targets_basis CHECK (char_length(interval_basis) BETWEEN 1 AND 64),
    CONSTRAINT forecast_snapshot_targets_finite CHECK (
        point NOT IN ('NaN', 'Infinity', '-Infinity')
        AND (lower_bound IS NULL OR lower_bound NOT IN ('NaN', 'Infinity', '-Infinity'))
        AND (upper_bound IS NULL OR upper_bound NOT IN ('NaN', 'Infinity', '-Infinity'))),
    CONSTRAINT forecast_snapshot_targets_band CHECK (
        (lower_bound IS NULL) = (upper_bound IS NULL) AND (lower_bound IS NULL OR lower_bound <= upper_bound))
);
CREATE INDEX forecast_snapshot_targets_owner_month
    ON jarvis_app.forecast_snapshot_targets (owner_id, target_month);

CREATE FUNCTION jarvis_app.forecast_snapshots_refuse_update() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'forecast snapshots are immutable' USING ERRCODE = 'restrict_violation';
END;
$$;
CREATE TRIGGER forecast_snapshots_immutable BEFORE UPDATE ON jarvis_app.forecast_snapshots
    FOR EACH ROW EXECUTE FUNCTION jarvis_app.forecast_snapshots_refuse_update();
CREATE TRIGGER forecast_snapshot_targets_immutable BEFORE UPDATE ON jarvis_app.forecast_snapshot_targets
    FOR EACH ROW EXECUTE FUNCTION jarvis_app.forecast_snapshots_refuse_update();
