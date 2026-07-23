CREATE TABLE IF NOT EXISTS charm_control.experiment_manifest_reviews (
    review_id uuid PRIMARY KEY,
    preflight_id uuid NOT NULL
        REFERENCES charm_control.experiment_manifest_preflights(preflight_id),
    arm_id uuid NOT NULL REFERENCES charm_control.experiment_arms(arm_id),
    execution_sha256 text NOT NULL CHECK (length(execution_sha256) = 64),
    frozen_manifest_sha256 text NOT NULL CHECK (length(frozen_manifest_sha256) = 64),
    execution_relative_path text NOT NULL,
    execution_byte_size bigint NOT NULL CHECK (execution_byte_size > 0),
    passed boolean NOT NULL,
    reasons jsonb NOT NULL,
    review jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (preflight_id,arm_id,execution_sha256)
);

CREATE INDEX IF NOT EXISTS experiment_manifest_reviews_arm_idx
    ON charm_control.experiment_manifest_reviews (arm_id,created_at DESC);
