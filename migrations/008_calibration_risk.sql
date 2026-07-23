CREATE TABLE IF NOT EXISTS charm_control.calibration_predictions (
    prediction_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    candidate_key text NOT NULL,
    sequence_number integer NOT NULL CHECK (sequence_number >= 0),
    model_family text NOT NULL,
    target_name text NOT NULL,
    nominal_coverage double precision NOT NULL CHECK (
        nominal_coverage > 0 AND nominal_coverage < 1
    ),
    feature_vector jsonb NOT NULL,
    point_prediction double precision NOT NULL,
    lower_bound double precision NOT NULL,
    upper_bound double precision NOT NULL,
    continuous_feasibility_probability double precision NOT NULL CHECK (
        continuous_feasibility_probability BETWEEN 0 AND 1
    ),
    categorical_feasibility_probability double precision NOT NULL CHECK (
        categorical_feasibility_probability BETWEEN 0 AND 1
    ),
    direct_joint_feasibility_probability double precision CHECK (
        direct_joint_feasibility_probability BETWEEN 0 AND 1
    ),
    joint_probabilities jsonb NOT NULL,
    selected_joint_rule text NOT NULL,
    selected_joint_probability double precision NOT NULL CHECK (
        selected_joint_probability BETWEEN 0 AND 1
    ),
    training_observation_ids jsonb NOT NULL,
    calibration_observation_ids jsonb NOT NULL,
    predicted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    observed_value double precision,
    observed_continuous_feasible boolean,
    observed_categorical_feasible boolean,
    observed_joint_feasible boolean,
    labeled_at timestamptz,
    UNIQUE(campaign_id, candidate_key, model_family, target_name, nominal_coverage),
    CHECK (lower_bound <= upper_bound),
    CHECK (
        (labeled_at IS NULL AND observed_value IS NULL
            AND observed_continuous_feasible IS NULL
            AND observed_categorical_feasible IS NULL
            AND observed_joint_feasible IS NULL)
        OR
        (labeled_at IS NOT NULL AND observed_value IS NOT NULL
            AND observed_continuous_feasible IS NOT NULL
            AND observed_categorical_feasible IS NOT NULL
            AND observed_joint_feasible IS NOT NULL
            AND labeled_at >= predicted_at)
    )
);

CREATE TABLE IF NOT EXISTS charm_control.calibration_reports (
    report_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    scored_candidates integer NOT NULL CHECK (scored_candidates >= 0),
    feasible_labels integer NOT NULL CHECK (feasible_labels >= 0),
    infeasible_labels integer NOT NULL CHECK (infeasible_labels >= 0),
    metrics jsonb NOT NULL,
    activation_gate jsonb NOT NULL,
    probabilistic_promotion_enabled boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS calibration_prediction_campaign_time_idx
    ON charm_control.calibration_predictions(campaign_id, predicted_at);
