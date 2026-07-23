CREATE TABLE IF NOT EXISTS charm_control.coordination_decisions (
    coordination_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    strategy text NOT NULL,
    budget_position integer NOT NULL CHECK (budget_position >= 0),
    knob_configuration jsonb NOT NULL,
    index_candidate_ids uuid[] NOT NULL DEFAULT '{}',
    component_scores jsonb NOT NULL,
    interaction_score double precision,
    selected_score double precision NOT NULL,
    training_observation_ids jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(campaign_id, strategy, budget_position)
);

CREATE TABLE IF NOT EXISTS charm_control.cost_observations (
    cost_observation_id uuid PRIMARY KEY,
    trial_id uuid NOT NULL REFERENCES charm_control.trials(trial_id),
    benchmark_seconds double precision NOT NULL CHECK (benchmark_seconds >= 0),
    restart_seconds double precision NOT NULL CHECK (restart_seconds >= 0),
    readiness_seconds double precision NOT NULL CHECK (readiness_seconds >= 0),
    index_build_seconds double precision NOT NULL CHECK (index_build_seconds >= 0),
    index_drop_seconds double precision NOT NULL CHECK (index_drop_seconds >= 0),
    index_build_wal_bytes numeric NOT NULL CHECK (index_build_wal_bytes >= 0),
    index_storage_bytes bigint NOT NULL CHECK (index_storage_bytes >= 0),
    configuration_churn integer NOT NULL CHECK (configuration_churn >= 0),
    raw_details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(trial_id)
);

CREATE TABLE IF NOT EXISTS charm_control.cost_decisions (
    cost_decision_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    candidate_key text NOT NULL,
    variant text NOT NULL,
    raw_components jsonb NOT NULL,
    normalization_reference jsonb NOT NULL,
    normalized_components jsonb NOT NULL,
    total_normalized_cost double precision NOT NULL CHECK (total_normalized_cost >= 0),
    expected_utility double precision NOT NULL,
    mandatory_f3_anchor boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(campaign_id, candidate_key, variant)
);
