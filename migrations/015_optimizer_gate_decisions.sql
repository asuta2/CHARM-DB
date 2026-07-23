CREATE TABLE IF NOT EXISTS charm_control.optimizer_gate_decisions (
    decision_id uuid PRIMARY KEY,
    recommendation_id uuid NOT NULL UNIQUE
        REFERENCES charm_control.multi_objective_recommendations(recommendation_id),
    calibration_report_id uuid REFERENCES charm_control.calibration_reports(report_id),
    fidelity_report_id uuid REFERENCES charm_control.fidelity_reports(report_id),
    probabilistic_gate_enabled boolean NOT NULL,
    early_stopping_enabled boolean NOT NULL,
    decision text NOT NULL CHECK (decision IN ('FULL_F3_REQUIRED','ADAPTIVE_ELIGIBLE')),
    reasons jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
