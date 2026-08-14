CREATE TABLE IF NOT EXISTS charm_control.experiment_restore_capability_assessments (
    assessment_id uuid PRIMARY KEY,
    protocol_id text NOT NULL CHECK (protocol_id = 'thesis-protocol-v2'),
    compose_project text NOT NULL,
    compose_service text NOT NULL,
    container_id text NOT NULL,
    image_digest text NOT NULL,
    volume_name text NOT NULL,
    volume_driver text NOT NULL,
    volume_mountpoint text NOT NULL,
    filesystem_type text NOT NULL,
    docker_engine jsonb NOT NULL,
    mechanisms jsonb NOT NULL,
    evidence jsonb NOT NULL,
    artifact_relative_path text NOT NULL,
    artifact_sha256 text NOT NULL CHECK (length(artifact_sha256) = 64),
    artifact_byte_size bigint NOT NULL CHECK (artifact_byte_size > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (artifact_sha256)
);

CREATE TABLE IF NOT EXISTS charm_control.experiment_physical_dataset_archives (
    archive_id uuid PRIMARY KEY,
    protocol_id text NOT NULL CHECK (protocol_id = 'thesis-protocol-v2'),
    baseline_id uuid NOT NULL
        REFERENCES charm_control.experiment_candidate_dataset_baselines(baseline_id),
    assessment_id uuid NOT NULL
        REFERENCES charm_control.experiment_restore_capability_assessments(assessment_id),
    preflight_id uuid NOT NULL
        REFERENCES charm_control.experiment_manifest_preflights(preflight_id),
    source_validation_id uuid NOT NULL
        REFERENCES charm_control.experiment_dataset_restore_validations(validation_id),
    volume_name text NOT NULL,
    image_digest text NOT NULL,
    archive_relative_path text NOT NULL,
    archive_sha256 text NOT NULL CHECK (length(archive_sha256) = 64),
    archive_byte_size bigint NOT NULL CHECK (archive_byte_size > 0),
    exact_core jsonb NOT NULL,
    physical_statistics jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('CREATING','VALIDATED','FAILED')),
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    duration_seconds double precision CHECK (
        duration_seconds IS NULL OR duration_seconds >= 0
    ),
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (status = 'CREATING' AND completed_at IS NULL AND duration_seconds IS NULL)
        OR (status IN ('VALIDATED','FAILED') AND completed_at IS NOT NULL
            AND duration_seconds IS NOT NULL)
    ),
    UNIQUE (baseline_id,archive_sha256)
);

CREATE INDEX IF NOT EXISTS physical_dataset_archives_preflight_idx
    ON charm_control.experiment_physical_dataset_archives(preflight_id,created_at DESC);

CREATE OR REPLACE FUNCTION charm_control.prevent_restore_capability_rewrite()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'restore capability evidence is immutable';
END
$$;

DROP TRIGGER IF EXISTS restore_capability_immutable
    ON charm_control.experiment_restore_capability_assessments;

CREATE TRIGGER restore_capability_immutable
BEFORE UPDATE OR DELETE ON charm_control.experiment_restore_capability_assessments
FOR EACH ROW
EXECUTE FUNCTION charm_control.prevent_restore_capability_rewrite();

CREATE OR REPLACE FUNCTION charm_control.validate_physical_archive_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status IN ('VALIDATED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'finalized physical archive evidence is immutable';
    END IF;
    IF OLD.status = 'CREATING' AND NEW.status NOT IN ('VALIDATED','FAILED') THEN
        RAISE EXCEPTION 'invalid physical archive transition % -> %',
            OLD.status, NEW.status;
    END IF;
    IF NEW.archive_relative_path IS DISTINCT FROM OLD.archive_relative_path
       OR NEW.archive_sha256 IS DISTINCT FROM OLD.archive_sha256
       OR NEW.archive_byte_size IS DISTINCT FROM OLD.archive_byte_size
       OR NEW.baseline_id IS DISTINCT FROM OLD.baseline_id
       OR NEW.assessment_id IS DISTINCT FROM OLD.assessment_id
       OR NEW.preflight_id IS DISTINCT FROM OLD.preflight_id
       OR NEW.source_validation_id IS DISTINCT FROM OLD.source_validation_id
       OR NEW.volume_name IS DISTINCT FROM OLD.volume_name
       OR NEW.image_digest IS DISTINCT FROM OLD.image_digest
       OR NEW.exact_core IS DISTINCT FROM OLD.exact_core
       OR NEW.physical_statistics IS DISTINCT FROM OLD.physical_statistics
       OR NEW.started_at IS DISTINCT FROM OLD.started_at
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'physical archive identity is immutable';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS physical_archive_transition_guard
    ON charm_control.experiment_physical_dataset_archives;

CREATE TRIGGER physical_archive_transition_guard
BEFORE UPDATE ON charm_control.experiment_physical_dataset_archives
FOR EACH ROW
EXECUTE FUNCTION charm_control.validate_physical_archive_transition();

CREATE TRIGGER physical_archive_delete_guard
BEFORE DELETE ON charm_control.experiment_physical_dataset_archives
FOR EACH ROW
EXECUTE FUNCTION charm_control.prevent_restore_capability_rewrite();
