CREATE TABLE IF NOT EXISTS charm_control.configuration_snapshots (
    snapshot_id uuid PRIMARY KEY,
    label text NOT NULL,
    postgres_version text NOT NULL,
    settings jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS charm_control.configuration_applications (
    application_id uuid PRIMARY KEY,
    snapshot_id uuid NOT NULL REFERENCES charm_control.configuration_snapshots(snapshot_id),
    requested_settings jsonb NOT NULL,
    verified_settings jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL,
    requires_restart boolean NOT NULL,
    apply_duration_seconds double precision,
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CHECK (status IN ('APPLYING','VERIFIED','FAILED','ROLLED_BACK'))
);

CREATE TABLE IF NOT EXISTS charm_control.rollbacks (
    rollback_id uuid PRIMARY KEY,
    application_id uuid NOT NULL REFERENCES charm_control.configuration_applications(application_id),
    reason text NOT NULL,
    restored_settings jsonb NOT NULL,
    verified boolean NOT NULL,
    duration_seconds double precision NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

