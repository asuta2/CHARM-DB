CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_default_reference_blocks (
    block_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL UNIQUE REFERENCES charm_control.campaigns(campaign_id),
    protocol_id text NOT NULL CHECK (protocol_id = 'thesis-protocol-v2'),
    evidence_role text NOT NULL CHECK (evidence_role = 'CALIBRATION'),
    manifest_sha256 text NOT NULL CHECK (length(manifest_sha256) = 64),
    preflight_id uuid NOT NULL
        REFERENCES charm_control.experiment_manifest_preflights(preflight_id),
    baseline_id uuid NOT NULL
        REFERENCES charm_control.experiment_candidate_dataset_baselines(baseline_id),
    benchmark_profile_id text NOT NULL CHECK (benchmark_profile_id <> ''),
    status text NOT NULL CHECK (status IN (
        'INITIAL_PLANNED','INITIAL_RUNNING','INITIAL_COMPLETE',
        'CONTINGENCY_PLANNED','CONTINGENCY_RUNNING','CONTINGENCY_COMPLETE',
        'PASSED','BLOCKED','FAILED'
    )),
    primary_seeds jsonb NOT NULL CHECK (
        jsonb_typeof(primary_seeds) = 'array' AND jsonb_array_length(primary_seeds) = 5
    ),
    contingency_seeds jsonb NOT NULL CHECK (
        jsonb_typeof(contingency_seeds) = 'array'
        AND jsonb_array_length(contingency_seeds) = 5
    ),
    acceptance_criteria jsonb NOT NULL CHECK (
        jsonb_typeof(acceptance_criteria) = 'object'
    ),
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

CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_default_reference_runs (
    run_id uuid PRIMARY KEY,
    block_id uuid NOT NULL
        REFERENCES charm_control.experiment_v2_default_reference_blocks(block_id),
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    sequence integer NOT NULL CHECK (sequence BETWEEN 1 AND 10),
    chronological_execution_index integer NOT NULL CHECK (
        chronological_execution_index BETWEEN 1 AND 10
    ),
    subphase text NOT NULL CHECK (subphase IN ('INITIAL','CONTINGENCY')),
    random_seed bigint NOT NULL,
    trial_id uuid UNIQUE REFERENCES charm_control.trials(trial_id),
    status text NOT NULL CHECK (status IN ('PLANNED','CREATED','COMPLETED','FAILED')),
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    UNIQUE (block_id,sequence),
    UNIQUE (block_id,chronological_execution_index),
    UNIQUE (block_id,random_seed),
    CHECK (
        (subphase = 'INITIAL' AND sequence BETWEEN 1 AND 5)
        OR (subphase = 'CONTINGENCY' AND sequence BETWEEN 6 AND 10)
    ),
    CHECK (
        (status = 'PLANNED' AND trial_id IS NULL AND completed_at IS NULL)
        OR (status = 'CREATED' AND trial_id IS NOT NULL AND completed_at IS NULL)
        OR (status IN ('COMPLETED','FAILED')
            AND trial_id IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS v2_default_reference_runs_block_sequence_idx
    ON charm_control.experiment_v2_default_reference_runs(block_id,sequence);

CREATE OR REPLACE FUNCTION charm_control.validate_v2_default_reference_block_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'INITIAL_PLANNED' AND NEW.status IN ('INITIAL_RUNNING','FAILED'))
        OR (OLD.status = 'INITIAL_RUNNING' AND NEW.status IN ('INITIAL_COMPLETE','FAILED'))
        OR (OLD.status = 'INITIAL_COMPLETE'
            AND NEW.status IN ('CONTINGENCY_PLANNED','PASSED','BLOCKED','FAILED'))
        OR (OLD.status = 'CONTINGENCY_PLANNED'
            AND NEW.status IN ('CONTINGENCY_RUNNING','FAILED'))
        OR (OLD.status = 'CONTINGENCY_RUNNING'
            AND NEW.status IN ('CONTINGENCY_COMPLETE','FAILED'))
        OR (OLD.status = 'CONTINGENCY_COMPLETE'
            AND NEW.status IN ('PASSED','BLOCKED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid v2 default-reference transition % -> %',
            OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('PASSED','BLOCKED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'finalized v2 default-reference block is immutable';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS v2_default_reference_block_transition_guard
    ON charm_control.experiment_v2_default_reference_blocks;

CREATE TRIGGER v2_default_reference_block_transition_guard
BEFORE UPDATE ON charm_control.experiment_v2_default_reference_blocks
FOR EACH ROW
EXECUTE FUNCTION charm_control.validate_v2_default_reference_block_transition();

CREATE OR REPLACE FUNCTION charm_control.validate_v2_default_reference_run_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'PLANNED' AND NEW.status = 'CREATED')
        OR (OLD.status = 'CREATED' AND NEW.status IN ('COMPLETED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid v2 default-reference run transition % -> %',
            OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('COMPLETED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'finalized v2 default-reference run is immutable';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS v2_default_reference_run_transition_guard
    ON charm_control.experiment_v2_default_reference_runs;

CREATE TRIGGER v2_default_reference_run_transition_guard
BEFORE UPDATE ON charm_control.experiment_v2_default_reference_runs
FOR EACH ROW
EXECUTE FUNCTION charm_control.validate_v2_default_reference_run_transition();
