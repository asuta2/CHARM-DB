CREATE TABLE IF NOT EXISTS charm_control.optimization_observations (
    observation_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    benchmark_trial_id uuid REFERENCES charm_control.trials(trial_id),
    application_id uuid REFERENCES charm_control.configuration_applications(application_id),
    method text NOT NULL,
    iteration integer NOT NULL CHECK (iteration >= 0),
    random_seed bigint NOT NULL,
    input_vector jsonb NOT NULL,
    configuration jsonb NOT NULL,
    throughput_tps double precision,
    p99_ms double precision,
    p99_margin_ms double precision,
    failures integer,
    feasible boolean NOT NULL,
    acquisition_name text,
    acquisition_value double precision,
    probability_feasible double precision,
    training_observation_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL,
    diagnostic_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (campaign_id, method, iteration),
    CHECK (status IN ('COMPLETED','FAILED','INFEASIBLE'))
);

CREATE UNIQUE INDEX IF NOT EXISTS optimization_campaign_configuration_idx
    ON charm_control.optimization_observations
    (campaign_id, md5(configuration::text))
    WHERE status = 'COMPLETED';
