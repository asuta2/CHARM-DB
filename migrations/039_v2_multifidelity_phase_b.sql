CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_multifidelity_phase_b_blocks (
    phase_b_block_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL UNIQUE REFERENCES charm_control.campaigns(campaign_id),
    protocol_id text NOT NULL CHECK (protocol_id = 'thesis-protocol-v2'),
    evidence_role text NOT NULL CHECK (evidence_role = 'SECONDARY'),
    manifest_sha256 text NOT NULL CHECK (length(manifest_sha256) = 64),
    phase_a_analysis_sha256 text NOT NULL CHECK (length(phase_a_analysis_sha256) = 64),
    preflight_id uuid NOT NULL REFERENCES charm_control.experiment_manifest_preflights(preflight_id),
    baseline_id uuid NOT NULL REFERENCES charm_control.experiment_candidate_dataset_baselines(baseline_id),
    benchmark_profile_id text NOT NULL CHECK (benchmark_profile_id <> ''),
    seed bigint NOT NULL CHECK (seed > 0),
    candidate_design_sha256 text NOT NULL CHECK (length(candidate_design_sha256) = 64),
    status text NOT NULL CHECK (status IN (
        'PLANNED','RUNNING','PAUSED_INFRASTRUCTURE','OBSERVATIONS_COMPLETE','ANALYZED','FAILED'
    )),
    analysis jsonb,
    analysis_sha256 text CHECK (analysis_sha256 IS NULL OR length(analysis_sha256) = 64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CHECK (
        (status IN ('ANALYZED','FAILED') AND completed_at IS NOT NULL)
        OR (status NOT IN ('ANALYZED','FAILED') AND completed_at IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_multifidelity_phase_b_runs (
    phase_b_run_id uuid PRIMARY KEY,
    phase_b_block_id uuid NOT NULL
        REFERENCES charm_control.experiment_v2_multifidelity_phase_b_blocks(phase_b_block_id),
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    physical_position integer NOT NULL CHECK (physical_position BETWEEN 1 AND 35),
    slot_kind text NOT NULL CHECK (slot_kind IN ('DEFAULT_CONTROL','CANDIDATE')),
    control_block integer NOT NULL CHECK (control_block BETWEEN 1 AND 5),
    candidate_position integer CHECK (candidate_position BETWEEN 1 AND 30),
    random_seed bigint NOT NULL CHECK (random_seed > 0),
    continuation_seed bigint CHECK (continuation_seed IS NULL OR continuation_seed > 0),
    candidate_vector jsonb CHECK (candidate_vector IS NULL OR jsonb_typeof(candidate_vector)='array'),
    requested_configuration jsonb NOT NULL CHECK (jsonb_typeof(requested_configuration)='object'),
    proposal_sha256 text NOT NULL CHECK (length(proposal_sha256)=64),
    status text NOT NULL CHECK (status IN (
        'PROPOSED','CREATED','RETRY_PENDING','COMPLETED',
        'CANDIDATE_FAILED','INFRASTRUCTURE_EXHAUSTED'
    )),
    infrastructure_attempts integer NOT NULL DEFAULT 0 CHECK (infrastructure_attempts BETWEEN 0 AND 3),
    promotion_baseline_tps double precision,
    promoted boolean,
    f2_throughput_tps double precision,
    f2_failures integer,
    reconnect_gap_seconds double precision,
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    UNIQUE (phase_b_block_id,physical_position),
    UNIQUE (phase_b_block_id,candidate_position),
    CHECK (
        (slot_kind='DEFAULT_CONTROL' AND candidate_position IS NULL
            AND continuation_seed IS NULL AND candidate_vector IS NULL)
        OR (slot_kind='CANDIDATE' AND candidate_position IS NOT NULL
            AND continuation_seed IS NOT NULL AND candidate_vector IS NOT NULL)
    ),
    CHECK (
        (status IN ('PROPOSED','CREATED','RETRY_PENDING') AND completed_at IS NULL)
        OR (status IN ('COMPLETED','CANDIDATE_FAILED','INFRASTRUCTURE_EXHAUSTED')
            AND completed_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_multifidelity_phase_b_attempts (
    phase_b_attempt_id uuid PRIMARY KEY,
    phase_b_run_id uuid NOT NULL
        REFERENCES charm_control.experiment_v2_multifidelity_phase_b_runs(phase_b_run_id),
    attempt_number integer NOT NULL CHECK (attempt_number BETWEEN 1 AND 3),
    trial_id uuid NOT NULL UNIQUE REFERENCES charm_control.trials(trial_id),
    status text NOT NULL CHECK (status IN (
        'CREATED','COMPLETED','CANDIDATE_FAILED','INFRASTRUCTURE_FAILED'
    )),
    failure_type text,
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    UNIQUE (phase_b_run_id,attempt_number),
    CHECK ((status='CREATED' AND completed_at IS NULL) OR
           (status<>'CREATED' AND completed_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS v2_multifidelity_phase_b_runs_position_idx
    ON charm_control.experiment_v2_multifidelity_phase_b_runs(phase_b_block_id,physical_position);

CREATE OR REPLACE FUNCTION charm_control.validate_v2_multifidelity_phase_b_block_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status='PLANNED' AND NEW.status IN ('RUNNING','FAILED')) OR
        (OLD.status='RUNNING' AND NEW.status IN
            ('PAUSED_INFRASTRUCTURE','OBSERVATIONS_COMPLETE','FAILED')) OR
        (OLD.status='PAUSED_INFRASTRUCTURE' AND NEW.status='FAILED') OR
        (OLD.status='OBSERVATIONS_COMPLETE' AND NEW.status IN ('ANALYZED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid v2 multifidelity Phase B block transition % -> %',
            OLD.status,NEW.status;
    END IF;
    IF OLD.status IN ('ANALYZED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'finalized v2 multifidelity Phase B block is immutable';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION charm_control.validate_v2_multifidelity_phase_b_run_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.phase_b_block_id,NEW.campaign_id,NEW.physical_position,NEW.slot_kind,
        NEW.control_block,NEW.candidate_position,NEW.random_seed,NEW.continuation_seed,
        NEW.candidate_vector,NEW.requested_configuration,NEW.proposal_sha256)
       IS DISTINCT FROM
       (OLD.phase_b_block_id,OLD.campaign_id,OLD.physical_position,OLD.slot_kind,
        OLD.control_block,OLD.candidate_position,OLD.random_seed,OLD.continuation_seed,
        OLD.candidate_vector,OLD.requested_configuration,OLD.proposal_sha256) THEN
        RAISE EXCEPTION 'v2 multifidelity Phase B slot identity is immutable';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status IN ('PROPOSED','RETRY_PENDING') AND NEW.status='CREATED') OR
        (OLD.status='CREATED' AND NEW.status IN
            ('COMPLETED','CANDIDATE_FAILED','RETRY_PENDING','INFRASTRUCTURE_EXHAUSTED'))
    ) THEN
        RAISE EXCEPTION 'invalid v2 multifidelity Phase B run transition % -> %',
            OLD.status,NEW.status;
    END IF;
    IF OLD.status IN ('COMPLETED','CANDIDATE_FAILED','INFRASTRUCTURE_EXHAUSTED')
       AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal v2 multifidelity Phase B run is immutable';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION charm_control.validate_v2_multifidelity_phase_b_attempt_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.phase_b_run_id,NEW.attempt_number,NEW.trial_id) IS DISTINCT FROM
       (OLD.phase_b_run_id,OLD.attempt_number,OLD.trial_id) THEN
        RAISE EXCEPTION 'v2 multifidelity Phase B attempt identity is immutable';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        OLD.status='CREATED' AND NEW.status IN
            ('COMPLETED','CANDIDATE_FAILED','INFRASTRUCTURE_FAILED')
    ) THEN
        RAISE EXCEPTION 'invalid v2 multifidelity Phase B attempt transition % -> %',
            OLD.status,NEW.status;
    END IF;
    IF OLD.status<>'CREATED' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal v2 multifidelity Phase B attempt is immutable';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS v2_multifidelity_phase_b_block_guard
    ON charm_control.experiment_v2_multifidelity_phase_b_blocks;
CREATE TRIGGER v2_multifidelity_phase_b_block_guard
BEFORE UPDATE ON charm_control.experiment_v2_multifidelity_phase_b_blocks
FOR EACH ROW EXECUTE FUNCTION charm_control.validate_v2_multifidelity_phase_b_block_transition();

DROP TRIGGER IF EXISTS v2_multifidelity_phase_b_run_guard
    ON charm_control.experiment_v2_multifidelity_phase_b_runs;
CREATE TRIGGER v2_multifidelity_phase_b_run_guard
BEFORE UPDATE ON charm_control.experiment_v2_multifidelity_phase_b_runs
FOR EACH ROW EXECUTE FUNCTION charm_control.validate_v2_multifidelity_phase_b_run_transition();

DROP TRIGGER IF EXISTS v2_multifidelity_phase_b_attempt_guard
    ON charm_control.experiment_v2_multifidelity_phase_b_attempts;
CREATE TRIGGER v2_multifidelity_phase_b_attempt_guard
BEFORE UPDATE ON charm_control.experiment_v2_multifidelity_phase_b_attempts
FOR EACH ROW EXECUTE FUNCTION charm_control.validate_v2_multifidelity_phase_b_attempt_transition();
