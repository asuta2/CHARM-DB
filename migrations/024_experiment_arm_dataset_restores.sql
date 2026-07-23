CREATE TABLE IF NOT EXISTS charm_control.experiment_arm_dataset_restores (
    arm_id uuid PRIMARY KEY REFERENCES charm_control.experiment_arms(arm_id),
    preflight_id uuid NOT NULL
        REFERENCES charm_control.experiment_manifest_preflights(preflight_id),
    validation_id uuid NOT NULL UNIQUE
        REFERENCES charm_control.experiment_dataset_restore_validations(validation_id),
    manifest_sha256 text NOT NULL CHECK (length(manifest_sha256) = 64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
