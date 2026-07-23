ALTER TABLE charm_control.experiment_manifest_preflights
    ADD COLUMN IF NOT EXISTS dataset_content_sha256 text,
    ADD CONSTRAINT experiment_manifest_preflights_content_sha256_check CHECK (
        dataset_content_sha256 IS NULL OR length(dataset_content_sha256) = 64
    );

CREATE TABLE IF NOT EXISTS charm_control.experiment_dataset_restore_validations (
    validation_id uuid PRIMARY KEY,
    preflight_id uuid NOT NULL
        REFERENCES charm_control.experiment_manifest_preflights(preflight_id),
    expected_snapshot_sha256 text NOT NULL,
    expected_schema_sha256 text NOT NULL,
    expected_content_sha256 text NOT NULL,
    observed_schema_sha256 text NOT NULL,
    observed_content_sha256 text NOT NULL,
    passed boolean NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS experiment_dataset_restore_validations_preflight_idx
    ON charm_control.experiment_dataset_restore_validations (preflight_id,created_at DESC);
