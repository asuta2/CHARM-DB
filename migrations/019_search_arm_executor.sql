CREATE TABLE IF NOT EXISTS charm_control.experiment_search_recommendations (
    recommendation_id uuid PRIMARY KEY,
    arm_id uuid NOT NULL REFERENCES charm_control.experiment_arms(arm_id),
    budget_position integer NOT NULL CHECK (budget_position >= 0),
    method text NOT NULL CHECK (
        method IN ('postgresql_default','random','sobol','standard_bo','constrained_bo')
    ),
    stage text NOT NULL CHECK (
        stage IN ('DEFAULT','RANDOM','SOBOL','BO_INITIALIZATION','STANDARD_BO','CONSTRAINED_BO')
    ),
    random_seed bigint NOT NULL,
    input_vector jsonb,
    configuration jsonb NOT NULL,
    training_observation_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    acquisition_name text,
    acquisition_value double precision,
    probability_feasible double precision,
    trial_id uuid UNIQUE REFERENCES charm_control.trials(trial_id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (arm_id,budget_position)
);

CREATE INDEX IF NOT EXISTS experiment_search_recommendations_arm_idx
    ON charm_control.experiment_search_recommendations (arm_id,budget_position);
