CREATE TABLE IF NOT EXISTS charm_control.experiment_f3_reference_blocks (
    block_id uuid PRIMARY KEY,
    preflight_id uuid NOT NULL UNIQUE
        REFERENCES charm_control.experiment_manifest_preflights(preflight_id),
    campaign_id uuid NOT NULL UNIQUE REFERENCES charm_control.campaigns(campaign_id),
    warmup_seconds integer NOT NULL CHECK (warmup_seconds >= 0),
    measurement_seconds integer NOT NULL CHECK (measurement_seconds > 0),
    concurrency integer NOT NULL CHECK (concurrency > 0),
    required_trials integer NOT NULL CHECK (required_trials >= 5),
    trial_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    completed_trials integer NOT NULL DEFAULT 0 CHECK (completed_trials >= 0),
    reference_wall_clock_seconds double precision,
    passed boolean NOT NULL DEFAULT false,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz
);
