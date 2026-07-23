CREATE TABLE IF NOT EXISTS charm_control.experiment_manifest_preflights (
    preflight_id uuid PRIMARY KEY,
    dataset_snapshot_sha256 text NOT NULL CHECK (length(dataset_snapshot_sha256) = 64),
    schema_sha256 text NOT NULL CHECK (length(schema_sha256) = 64),
    snapshot_relative_path text NOT NULL,
    snapshot_byte_size bigint NOT NULL CHECK (snapshot_byte_size > 0),
    evidence_relative_path text NOT NULL,
    evidence_sha256 text NOT NULL CHECK (length(evidence_sha256) = 64),
    evidence_byte_size bigint NOT NULL CHECK (evidence_byte_size > 0),
    eligible boolean NOT NULL,
    unresolved jsonb NOT NULL,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (evidence_sha256)
);

CREATE INDEX IF NOT EXISTS experiment_manifest_preflights_time_idx
    ON charm_control.experiment_manifest_preflights (created_at DESC);
