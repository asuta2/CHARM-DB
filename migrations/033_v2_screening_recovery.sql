CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_restore_stability_blocks (
    validation_block_id uuid PRIMARY KEY,
    source_screening_block_id uuid NOT NULL UNIQUE
        REFERENCES charm_control.experiment_v2_screening_blocks(block_id),
    protocol_id text NOT NULL CHECK (protocol_id = 'thesis-protocol-v2'),
    evidence_role text NOT NULL CHECK (evidence_role = 'INFRASTRUCTURE'),
    manifest_sha256 text NOT NULL CHECK (length(manifest_sha256) = 64),
    mitigation_sha256 text NOT NULL CHECK (length(mitigation_sha256) = 64),
    status text NOT NULL CHECK (status IN ('PLANNED','RUNNING','PASSED','FAILED')),
    required_repetitions integer NOT NULL CHECK (required_repetitions = 3),
    result jsonb,
    result_sha256 text CHECK (result_sha256 IS NULL OR length(result_sha256) = 64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CHECK (
        (status IN ('PASSED','FAILED') AND completed_at IS NOT NULL)
        OR (status NOT IN ('PASSED','FAILED') AND completed_at IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_restore_stability_runs (
    validation_run_id uuid PRIMARY KEY,
    validation_block_id uuid NOT NULL
        REFERENCES charm_control.experiment_v2_restore_stability_blocks(validation_block_id),
    sequence integer NOT NULL CHECK (sequence BETWEEN 1 AND 3),
    status text NOT NULL CHECK (status IN ('PLANNED','RUNNING','PASSED','FAILED')),
    restore_validation_id uuid
        REFERENCES charm_control.experiment_dataset_restore_validations(validation_id),
    duration_seconds double precision CHECK (
        duration_seconds IS NULL OR duration_seconds >= 0
    ),
    verification_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    completed_at timestamptz,
    UNIQUE (validation_block_id,sequence),
    CHECK (
        (status = 'PLANNED' AND restore_validation_id IS NULL
            AND started_at IS NULL AND completed_at IS NULL)
        OR (status = 'RUNNING' AND started_at IS NOT NULL AND completed_at IS NULL)
        OR (status = 'PASSED' AND restore_validation_id IS NOT NULL
            AND started_at IS NOT NULL AND completed_at IS NOT NULL)
        OR (status = 'FAILED' AND started_at IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_screening_recovery_blocks (
    recovery_block_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL UNIQUE REFERENCES charm_control.campaigns(campaign_id),
    source_screening_block_id uuid NOT NULL UNIQUE
        REFERENCES charm_control.experiment_v2_screening_blocks(block_id),
    restore_validation_block_id uuid NOT NULL UNIQUE
        REFERENCES charm_control.experiment_v2_restore_stability_blocks(validation_block_id),
    protocol_id text NOT NULL CHECK (protocol_id = 'thesis-protocol-v2'),
    evidence_role text NOT NULL CHECK (evidence_role = 'CALIBRATION'),
    manifest_sha256 text NOT NULL CHECK (length(manifest_sha256) = 64),
    source_manifest_sha256 text NOT NULL CHECK (length(source_manifest_sha256) = 64),
    preflight_id uuid NOT NULL
        REFERENCES charm_control.experiment_manifest_preflights(preflight_id),
    baseline_id uuid NOT NULL
        REFERENCES charm_control.experiment_candidate_dataset_baselines(baseline_id),
    benchmark_profile_id text NOT NULL CHECK (benchmark_profile_id <> ''),
    status text NOT NULL CHECK (status IN (
        'RECOVERY_PLANNED','RECOVERY_RUNNING','INITIAL_COMPLETE',
        'OAT_PLANNED','OAT_RUNNING','OAT_COMPLETE','PASSED','BLOCKED','FAILED'
    )),
    recovery_schedule_sha256 text NOT NULL CHECK (length(recovery_schedule_sha256) = 64),
    analysis_plan jsonb NOT NULL CHECK (jsonb_typeof(analysis_plan) = 'object'),
    initial_analysis jsonb,
    initial_analysis_sha256 text CHECK (
        initial_analysis_sha256 IS NULL OR length(initial_analysis_sha256) = 64
    ),
    final_analysis jsonb,
    final_analysis_sha256 text CHECK (
        final_analysis_sha256 IS NULL OR length(final_analysis_sha256) = 64
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CHECK (
        (status IN ('PASSED','BLOCKED','FAILED') AND completed_at IS NOT NULL)
        OR (status NOT IN ('PASSED','BLOCKED','FAILED') AND completed_at IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_screening_recovery_runs (
    recovery_run_id uuid PRIMARY KEY,
    recovery_block_id uuid NOT NULL
        REFERENCES charm_control.experiment_v2_screening_recovery_blocks(recovery_block_id),
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    source_run_id uuid UNIQUE
        REFERENCES charm_control.experiment_v2_screening_runs(run_id),
    sequence integer NOT NULL CHECK (sequence BETWEEN 34 AND 41),
    chronological_execution_index integer NOT NULL CHECK (
        chronological_execution_index BETWEEN 34 AND 41
    ),
    phase text NOT NULL CHECK (phase IN ('INITIAL','OAT')),
    evaluation_kind text NOT NULL CHECK (
        evaluation_kind IN ('SOBOL','DEFAULT_CONTROL','OAT_LOW','OAT_HIGH')
    ),
    sobol_index integer CHECK (sobol_index = 32),
    oat_parameter text,
    random_seed bigint NOT NULL CHECK (random_seed > 0),
    requested_configuration jsonb NOT NULL CHECK (
        jsonb_typeof(requested_configuration) = 'object'
    ),
    trial_id uuid UNIQUE REFERENCES charm_control.trials(trial_id),
    status text NOT NULL CHECK (status IN ('PLANNED','CREATED','COMPLETED','FAILED')),
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    UNIQUE (recovery_block_id,sequence),
    UNIQUE (recovery_block_id,chronological_execution_index),
    UNIQUE (recovery_block_id,oat_parameter,evaluation_kind),
    CHECK (
        (phase = 'INITIAL' AND sequence IN (34,35)
            AND chronological_execution_index = sequence
            AND source_run_id IS NOT NULL AND oat_parameter IS NULL
            AND ((sequence = 34 AND evaluation_kind = 'SOBOL' AND sobol_index = 32)
              OR (sequence = 35 AND evaluation_kind = 'DEFAULT_CONTROL'
                  AND sobol_index IS NULL)))
        OR (phase = 'OAT' AND sequence BETWEEN 36 AND 41
            AND chronological_execution_index = sequence
            AND source_run_id IS NULL AND sobol_index IS NULL
            AND evaluation_kind IN ('OAT_LOW','OAT_HIGH')
            AND oat_parameter IS NOT NULL)
    ),
    CHECK (
        (status = 'PLANNED' AND trial_id IS NULL AND completed_at IS NULL)
        OR (status = 'CREATED' AND trial_id IS NOT NULL AND completed_at IS NULL)
        OR (status IN ('COMPLETED','FAILED')
            AND trial_id IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE OR REPLACE FUNCTION charm_control.validate_v2_restore_stability_block_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'PLANNED' AND NEW.status IN ('RUNNING','FAILED'))
        OR (OLD.status = 'RUNNING' AND NEW.status IN ('PASSED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid v2 restore-stability transition % -> %',
            OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('PASSED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'finalized v2 restore-stability block is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION charm_control.validate_v2_restore_stability_run_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'PLANNED' AND NEW.status = 'RUNNING')
        OR (OLD.status = 'RUNNING' AND NEW.status IN ('PASSED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid v2 restore-stability run transition % -> %',
            OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('PASSED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'finalized v2 restore-stability run is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION charm_control.validate_v2_screening_recovery_block_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'RECOVERY_PLANNED' AND NEW.status IN ('RECOVERY_RUNNING','FAILED'))
        OR (OLD.status = 'RECOVERY_RUNNING' AND NEW.status IN ('INITIAL_COMPLETE','FAILED'))
        OR (OLD.status = 'INITIAL_COMPLETE'
            AND NEW.status IN ('OAT_PLANNED','PASSED','BLOCKED','FAILED'))
        OR (OLD.status = 'OAT_PLANNED' AND NEW.status IN ('OAT_RUNNING','FAILED'))
        OR (OLD.status = 'OAT_RUNNING' AND NEW.status IN ('OAT_COMPLETE','FAILED'))
        OR (OLD.status = 'OAT_COMPLETE' AND NEW.status IN ('PASSED','BLOCKED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid v2 screening-recovery transition % -> %',
            OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('PASSED','BLOCKED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'finalized v2 screening-recovery block is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION charm_control.validate_v2_screening_recovery_run_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'PLANNED' AND NEW.status = 'CREATED')
        OR (OLD.status = 'CREATED' AND NEW.status IN ('COMPLETED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid v2 screening-recovery run transition % -> %',
            OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('COMPLETED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'finalized v2 screening-recovery run is immutable';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS v2_restore_stability_block_transition_guard
    ON charm_control.experiment_v2_restore_stability_blocks;
CREATE TRIGGER v2_restore_stability_block_transition_guard
BEFORE UPDATE ON charm_control.experiment_v2_restore_stability_blocks
FOR EACH ROW
EXECUTE FUNCTION charm_control.validate_v2_restore_stability_block_transition();

DROP TRIGGER IF EXISTS v2_restore_stability_run_transition_guard
    ON charm_control.experiment_v2_restore_stability_runs;
CREATE TRIGGER v2_restore_stability_run_transition_guard
BEFORE UPDATE ON charm_control.experiment_v2_restore_stability_runs
FOR EACH ROW
EXECUTE FUNCTION charm_control.validate_v2_restore_stability_run_transition();

DROP TRIGGER IF EXISTS v2_screening_recovery_block_transition_guard
    ON charm_control.experiment_v2_screening_recovery_blocks;
CREATE TRIGGER v2_screening_recovery_block_transition_guard
BEFORE UPDATE ON charm_control.experiment_v2_screening_recovery_blocks
FOR EACH ROW
EXECUTE FUNCTION charm_control.validate_v2_screening_recovery_block_transition();

DROP TRIGGER IF EXISTS v2_screening_recovery_run_transition_guard
    ON charm_control.experiment_v2_screening_recovery_runs;
CREATE TRIGGER v2_screening_recovery_run_transition_guard
BEFORE UPDATE ON charm_control.experiment_v2_screening_recovery_runs
FOR EACH ROW
EXECUTE FUNCTION charm_control.validate_v2_screening_recovery_run_transition();
