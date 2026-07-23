CREATE TABLE IF NOT EXISTS charm_control.fidelity_observations (
    fidelity_observation_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    candidate_key text NOT NULL,
    configuration jsonb NOT NULL,
    fidelity integer NOT NULL CHECK (fidelity BETWEEN 0 AND 4),
    replicate integer NOT NULL DEFAULT 0,
    benchmark_trial_id uuid REFERENCES charm_control.trials(trial_id),
    planner_cost double precision,
    throughput_tps double precision,
    p95_ms double precision,
    p99_ms double precision,
    failures integer,
    feasible boolean NOT NULL,
    wall_clock_seconds double precision NOT NULL,
    evidence_kind text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(campaign_id, candidate_key, fidelity, replicate)
);

CREATE TABLE IF NOT EXISTS charm_control.promotion_decisions (
    promotion_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    candidate_key text NOT NULL,
    from_fidelity integer NOT NULL,
    to_fidelity integer NOT NULL,
    promoted boolean NOT NULL,
    decision_rule text NOT NULL,
    decision_inputs jsonb NOT NULL,
    retrospective_actual_good boolean,
    classification text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(campaign_id, candidate_key, from_fidelity, to_fidelity)
);

CREATE TABLE IF NOT EXISTS charm_control.early_stop_decisions (
    early_stop_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    candidate_key text NOT NULL,
    fidelity integer NOT NULL,
    protected_seconds double precision NOT NULL,
    observed_throughput_tps double precision NOT NULL,
    baseline_throughput_tps double precision NOT NULL,
    stopped boolean NOT NULL,
    reason text NOT NULL,
    retrospective_false_stop boolean,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS charm_control.fidelity_reports (
    report_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    paired_candidates integer NOT NULL,
    spearman_correlation double precision,
    kendall_correlation double precision,
    mean_f2_f3_bias_tps double precision,
    promotion_true_positive integer NOT NULL,
    promotion_false_positive integer NOT NULL,
    promotion_true_negative integer NOT NULL,
    promotion_false_negative integer NOT NULL,
    early_stopping_enabled boolean NOT NULL,
    full_evaluations_avoided integer NOT NULL,
    measured_wall_clock_seconds double precision NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS fidelity_campaign_candidate_idx
    ON charm_control.fidelity_observations(campaign_id, candidate_key, fidelity);
