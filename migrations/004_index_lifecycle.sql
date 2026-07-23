CREATE TABLE IF NOT EXISTS charm_control.index_candidates (
    candidate_id uuid PRIMARY KEY,
    definition_hash text NOT NULL UNIQUE,
    index_name text NOT NULL UNIQUE,
    schema_name text NOT NULL,
    table_name text NOT NULL,
    access_method text NOT NULL DEFAULT 'btree',
    key_columns jsonb NOT NULL,
    include_columns jsonb NOT NULL DEFAULT '[]'::jsonb,
    predicate_sql text,
    expression_sql text,
    normalized_sql text NOT NULL,
    state text NOT NULL,
    provenance jsonb NOT NULL,
    managed boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (state IN ('PROPOSED','VALIDATED','HYPOTHETICALLY_EVALUATED','BUILDING',
                     'ACTIVE','MEASURED','RETAINED','REJECTED','DROPPING','DROPPED'))
);

CREATE TABLE IF NOT EXISTS charm_control.index_lifecycle_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    candidate_id uuid NOT NULL REFERENCES charm_control.index_candidates(candidate_id),
    from_state text,
    to_state text NOT NULL,
    reason text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS charm_control.index_measurements (
    measurement_id uuid PRIMARY KEY,
    candidate_id uuid NOT NULL REFERENCES charm_control.index_candidates(candidate_id),
    build_duration_seconds double precision NOT NULL,
    build_wal_bytes numeric NOT NULL,
    index_size_bytes bigint NOT NULL,
    before_median_ms double precision NOT NULL,
    after_median_ms double precision NOT NULL,
    post_drop_median_ms double precision,
    before_plan jsonb NOT NULL,
    after_plan jsonb NOT NULL,
    index_used boolean NOT NULL,
    paired_query_parameters jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS charm_control.trial_actions (
    action_id uuid PRIMARY KEY,
    trial_id uuid NOT NULL REFERENCES charm_control.trials(trial_id),
    knob_configuration jsonb NOT NULL DEFAULT '{}'::jsonb,
    proposed_index_ids uuid[] NOT NULL DEFAULT '{}',
    existing_indexes jsonb NOT NULL DEFAULT '[]'::jsonb,
    indexes_created uuid[] NOT NULL DEFAULT '{}',
    indexes_removed uuid[] NOT NULL DEFAULT '{}',
    operational_cost jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS index_events_candidate_time_idx
    ON charm_control.index_lifecycle_events(candidate_id, occurred_at);
