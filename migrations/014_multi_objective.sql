CREATE TABLE IF NOT EXISTS charm_control.multi_objective_runs (
    run_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL UNIQUE REFERENCES charm_control.campaigns(campaign_id),
    random_seed bigint NOT NULL,
    reference_point jsonb NOT NULL,
    objective_definition jsonb NOT NULL,
    constraint_definition jsonb NOT NULL,
    candidate_pool_size integer NOT NULL CHECK (candidate_pool_size > 0),
    sample_count integer NOT NULL CHECK (sample_count > 0),
    software_versions jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('RUNNING','COMPLETED','FAILED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS charm_control.multi_objective_recommendations (
    recommendation_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES charm_control.multi_objective_runs(run_id),
    observation_id uuid REFERENCES charm_control.optimization_observations(observation_id),
    iteration integer NOT NULL CHECK (iteration >= 0),
    method text NOT NULL CHECK (method IN ('SOBOL','qLogNEHVI','qLogNParEGO','F4_VALIDATION')),
    input_vector jsonb NOT NULL,
    configuration jsonb NOT NULL,
    workload_context jsonb NOT NULL,
    fidelity integer NOT NULL CHECK (fidelity BETWEEN 0 AND 4),
    pool_seed bigint NOT NULL,
    acquisition_value double precision,
    probability_feasible double precision,
    training_observation_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(run_id, iteration, method)
);

ALTER TABLE charm_control.optimization_observations
    ADD COLUMN IF NOT EXISTS workload_context jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS fidelity integer NOT NULL DEFAULT 3 CHECK (fidelity BETWEEN 0 AND 4),
    ADD COLUMN IF NOT EXISTS recommendation_id uuid
        REFERENCES charm_control.multi_objective_recommendations(recommendation_id);

DROP INDEX IF EXISTS charm_control.optimization_campaign_configuration_idx;

CREATE TABLE IF NOT EXISTS charm_control.pareto_snapshots (
    snapshot_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES charm_control.multi_objective_runs(run_id),
    iteration integer NOT NULL CHECK (iteration >= 0),
    reference_point jsonb NOT NULL,
    feasible_observation_ids jsonb NOT NULL,
    pareto_observation_ids jsonb NOT NULL,
    hypervolume double precision NOT NULL CHECK (hypervolume >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(run_id, iteration)
);

CREATE TABLE IF NOT EXISTS charm_control.champion_selections (
    champion_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES charm_control.multi_objective_runs(run_id),
    selected_observation_id uuid NOT NULL
        REFERENCES charm_control.optimization_observations(observation_id),
    rule text NOT NULL,
    f4_observation_ids jsonb NOT NULL,
    context_scores jsonb NOT NULL,
    robust_score double precision,
    worst_p99_ms double precision,
    status text NOT NULL CHECK (status IN ('CANDIDATE','VALIDATED','REJECTED')),
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(run_id)
);

CREATE INDEX IF NOT EXISTS multi_recommendations_run_iteration_idx
    ON charm_control.multi_objective_recommendations(run_id, iteration);
CREATE INDEX IF NOT EXISTS pareto_snapshots_run_iteration_idx
    ON charm_control.pareto_snapshots(run_id, iteration DESC);
