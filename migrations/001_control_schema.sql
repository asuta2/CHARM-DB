CREATE SCHEMA IF NOT EXISTS charm_control;

CREATE TABLE IF NOT EXISTS charm_control.schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    sha256 text NOT NULL
);

CREATE TABLE IF NOT EXISTS charm_control.campaigns (
    campaign_id uuid PRIMARY KEY,
    name text NOT NULL,
    mode text NOT NULL,
    status text NOT NULL,
    objective_definition jsonb NOT NULL,
    constraint_definition jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (status IN ('CREATED','RUNNING','PAUSED','STOPPING','STOPPED','COMPLETED','FAILED'))
);

CREATE TABLE IF NOT EXISTS charm_control.trials (
    trial_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    parent_trial_id uuid REFERENCES charm_control.trials(trial_id),
    state text NOT NULL,
    benchmark_profile text NOT NULL,
    fidelity integer NOT NULL CHECK (fidelity BETWEEN 0 AND 4),
    random_seed bigint NOT NULL,
    requested_configuration jsonb NOT NULL DEFAULT '{}'::jsonb,
    active_configuration jsonb NOT NULL DEFAULT '{}'::jsonb,
    objective_values jsonb,
    constraint_values jsonb,
    software_versions jsonb NOT NULL DEFAULT '{}'::jsonb,
    host_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    failure_type text,
    diagnostic_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS charm_control.trial_transitions (
    transition_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trial_id uuid NOT NULL REFERENCES charm_control.trials(trial_id),
    from_state text,
    to_state text NOT NULL,
    reason text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS charm_control.metric_snapshots (
    snapshot_id uuid PRIMARY KEY,
    trial_id uuid NOT NULL REFERENCES charm_control.trials(trial_id),
    phase text NOT NULL,
    source text NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    payload jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS charm_control.artifacts (
    artifact_id uuid PRIMARY KEY,
    trial_id uuid NOT NULL REFERENCES charm_control.trials(trial_id),
    kind text NOT NULL,
    relative_path text NOT NULL,
    sha256 text NOT NULL,
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (trial_id, relative_path)
);

CREATE INDEX IF NOT EXISTS trials_campaign_created_idx
    ON charm_control.trials (campaign_id, created_at);
CREATE INDEX IF NOT EXISTS transitions_trial_time_idx
    ON charm_control.trial_transitions (trial_id, occurred_at);

