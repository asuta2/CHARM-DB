CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_primary_restore_soaks (
    restore_soak_id uuid PRIMARY KEY,
    protocol_id text NOT NULL CHECK (protocol_id = 'thesis-protocol-v2'),
    evidence_role text NOT NULL CHECK (evidence_role = 'INFRASTRUCTURE'),
    manifest_sha256 text NOT NULL CHECK (length(manifest_sha256) = 64),
    contract_sha256 text NOT NULL CHECK (length(contract_sha256) = 64),
    preflight_id uuid NOT NULL
        REFERENCES charm_control.experiment_manifest_preflights(preflight_id),
    baseline_id uuid NOT NULL
        REFERENCES charm_control.experiment_candidate_dataset_baselines(baseline_id),
    restore_mechanism text NOT NULL CHECK (restore_mechanism = 'logical-restore'),
    required_repetitions integer NOT NULL CHECK (required_repetitions BETWEEN 15 AND 20),
    artifact_directory text NOT NULL CHECK (artifact_directory <> ''),
    status text NOT NULL CHECK (status IN ('PLANNED','RUNNING','PASSED','FAILED')),
    result jsonb,
    result_sha256 text CHECK (result_sha256 IS NULL OR length(result_sha256) = 64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CHECK (
        (status IN ('PASSED','FAILED') AND completed_at IS NOT NULL)
        OR (status IN ('PLANNED','RUNNING') AND completed_at IS NULL)
    ),
    CHECK (
        (status = 'PASSED' AND result IS NOT NULL AND result_sha256 IS NOT NULL)
        OR status <> 'PASSED'
    )
);

CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_primary_restore_soak_runs (
    restore_soak_run_id uuid PRIMARY KEY,
    restore_soak_id uuid NOT NULL
        REFERENCES charm_control.experiment_v2_primary_restore_soaks(restore_soak_id),
    sequence integer NOT NULL CHECK (sequence BETWEEN 1 AND 20),
    status text NOT NULL CHECK (status IN ('PLANNED','RUNNING','PASSED','FAILED')),
    restore_validation_id uuid
        REFERENCES charm_control.experiment_dataset_restore_validations(validation_id),
    duration_seconds double precision CHECK (
        duration_seconds IS NULL OR duration_seconds >= 0
    ),
    verification_details jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(verification_details) = 'object'),
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(failure_details) = 'object'),
    started_at timestamptz,
    completed_at timestamptz,
    UNIQUE (restore_soak_id,sequence),
    CHECK (
        (status = 'PLANNED' AND started_at IS NULL AND completed_at IS NULL)
        OR (status = 'RUNNING' AND started_at IS NOT NULL AND completed_at IS NULL)
        OR (status IN ('PASSED','FAILED') AND started_at IS NOT NULL
            AND completed_at IS NOT NULL)
    ),
    CHECK (
        (status = 'PASSED' AND restore_validation_id IS NOT NULL)
        OR status <> 'PASSED'
    )
);

CREATE INDEX IF NOT EXISTS v2_primary_restore_soaks_contract_idx
    ON charm_control.experiment_v2_primary_restore_soaks(
        contract_sha256,status,completed_at DESC
    );
CREATE INDEX IF NOT EXISTS v2_primary_restore_soak_runs_sequence_idx
    ON charm_control.experiment_v2_primary_restore_soak_runs(restore_soak_id,sequence);

CREATE OR REPLACE FUNCTION charm_control.validate_v2_primary_restore_soak_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF (
        NEW.protocol_id,NEW.evidence_role,NEW.manifest_sha256,NEW.contract_sha256,
        NEW.preflight_id,NEW.baseline_id,NEW.restore_mechanism,
        NEW.required_repetitions,NEW.artifact_directory
    ) IS DISTINCT FROM (
        OLD.protocol_id,OLD.evidence_role,OLD.manifest_sha256,OLD.contract_sha256,
        OLD.preflight_id,OLD.baseline_id,OLD.restore_mechanism,
        OLD.required_repetitions,OLD.artifact_directory
    ) THEN
        RAISE EXCEPTION 'v2 primary restore-soak identity is immutable';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'PLANNED' AND NEW.status IN ('RUNNING','FAILED'))
        OR (OLD.status = 'RUNNING' AND NEW.status IN ('PASSED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid v2 primary restore-soak transition % -> %',
            OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('PASSED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal v2 primary restore soak is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION charm_control.validate_v2_primary_restore_soak_run_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF (NEW.restore_soak_id,NEW.sequence) IS DISTINCT FROM
       (OLD.restore_soak_id,OLD.sequence) THEN
        RAISE EXCEPTION 'v2 primary restore-soak run identity is immutable';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'PLANNED' AND NEW.status IN ('RUNNING','FAILED'))
        OR (OLD.status = 'RUNNING' AND NEW.status IN ('PASSED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid v2 primary restore-soak run transition % -> %',
            OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('PASSED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal v2 primary restore-soak run is immutable';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS v2_primary_restore_soak_transition_guard
    ON charm_control.experiment_v2_primary_restore_soaks;
CREATE TRIGGER v2_primary_restore_soak_transition_guard
BEFORE UPDATE ON charm_control.experiment_v2_primary_restore_soaks
FOR EACH ROW EXECUTE FUNCTION charm_control.validate_v2_primary_restore_soak_transition();

DROP TRIGGER IF EXISTS v2_primary_restore_soak_run_transition_guard
    ON charm_control.experiment_v2_primary_restore_soak_runs;
CREATE TRIGGER v2_primary_restore_soak_run_transition_guard
BEFORE UPDATE ON charm_control.experiment_v2_primary_restore_soak_runs
FOR EACH ROW EXECUTE FUNCTION charm_control.validate_v2_primary_restore_soak_run_transition();
