CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_primary_blocks (
    primary_block_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL UNIQUE REFERENCES charm_control.campaigns(campaign_id),
    protocol_id text NOT NULL CHECK (protocol_id = 'thesis-protocol-v2'),
    evidence_role text NOT NULL CHECK (evidence_role = 'PRIMARY'),
    manifest_sha256 text NOT NULL CHECK (length(manifest_sha256) = 64),
    preflight_id uuid NOT NULL
        REFERENCES charm_control.experiment_manifest_preflights(preflight_id),
    baseline_id uuid NOT NULL
        REFERENCES charm_control.experiment_candidate_dataset_baselines(baseline_id),
    benchmark_profile_id text NOT NULL CHECK (benchmark_profile_id <> ''),
    schedule_sha256 text NOT NULL CHECK (length(schedule_sha256) = 64),
    candidate_design_sha256 text NOT NULL CHECK (length(candidate_design_sha256) = 64),
    status text NOT NULL CHECK (status IN (
        'PLANNED','RUNNING','PAUSED_INFRASTRUCTURE',
        'OBSERVATIONS_COMPLETE','ANALYZED','FAILED'
    )),
    retry_policy jsonb NOT NULL CHECK (jsonb_typeof(retry_policy) = 'object'),
    drift_interpretation jsonb NOT NULL CHECK (jsonb_typeof(drift_interpretation) = 'object'),
    analysis jsonb,
    analysis_sha256 text CHECK (analysis_sha256 IS NULL OR length(analysis_sha256) = 64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CHECK (
        (status IN ('ANALYZED','FAILED') AND completed_at IS NOT NULL)
        OR (status NOT IN ('ANALYZED','FAILED') AND completed_at IS NULL)
    ),
    CHECK (
        (status = 'ANALYZED' AND analysis IS NOT NULL AND analysis_sha256 IS NOT NULL)
        OR (status <> 'ANALYZED')
    )
);

CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_primary_runs (
    primary_run_id uuid PRIMARY KEY,
    primary_block_id uuid NOT NULL
        REFERENCES charm_control.experiment_v2_primary_blocks(primary_block_id),
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    global_position integer NOT NULL CHECK (global_position BETWEEN 1 AND 393),
    seed_index integer NOT NULL CHECK (seed_index BETWEEN 1 AND 3),
    seed bigint NOT NULL CHECK (seed > 0),
    within_seed_position integer NOT NULL CHECK (within_seed_position BETWEEN 1 AND 131),
    evaluation_role text NOT NULL CHECK (evaluation_role IN (
        'DEFAULT_CONTROL','CANDIDATE','BO_SHARED_INITIAL'
    )),
    method text NOT NULL CHECK (method IN (
        'postgresql_default','random','sobol','bo_shared_initial',
        'bo_qlognei_throughput','bo_qlognparego_multiobjective',
        'bo_qlognehvi_multiobjective'
    )),
    budget_position integer CHECK (budget_position BETWEEN 1 AND 30),
    shared_with_methods jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(shared_with_methods) = 'array'),
    random_seed bigint NOT NULL CHECK (random_seed > 0),
    candidate_vector jsonb CHECK (
        candidate_vector IS NULL OR jsonb_typeof(candidate_vector) = 'array'
    ),
    requested_configuration jsonb CHECK (
        requested_configuration IS NULL OR jsonb_typeof(requested_configuration) = 'object'
    ),
    proposal_sha256 text CHECK (proposal_sha256 IS NULL OR length(proposal_sha256) = 64),
    acquisition_name text,
    acquisition_value double precision,
    status text NOT NULL CHECK (status IN (
        'PLANNED','PROPOSED','CREATED','RETRY_PENDING','COMPLETED',
        'CANDIDATE_FAILED','INFRASTRUCTURE_EXHAUSTED'
    )),
    infrastructure_attempts integer NOT NULL DEFAULT 0 CHECK (
        infrastructure_attempts BETWEEN 0 AND 3
    ),
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    UNIQUE (primary_block_id,global_position),
    UNIQUE (primary_block_id,seed_index,within_seed_position),
    UNIQUE (primary_block_id,seed,method,budget_position),
    CHECK (
        (evaluation_role = 'DEFAULT_CONTROL' AND method = 'postgresql_default'
            AND budget_position IS NULL AND shared_with_methods = '[]'::jsonb)
        OR (evaluation_role = 'BO_SHARED_INITIAL' AND method = 'bo_shared_initial'
            AND budget_position BETWEEN 1 AND 12
            AND jsonb_array_length(shared_with_methods) = 3)
        OR (evaluation_role = 'CANDIDATE' AND method NOT IN (
                'postgresql_default','bo_shared_initial'
            ) AND budget_position IS NOT NULL AND shared_with_methods = '[]'::jsonb)
    ),
    CHECK (
        (status = 'PLANNED' AND candidate_vector IS NULL
            AND requested_configuration IS NULL AND proposal_sha256 IS NULL
            AND completed_at IS NULL)
        OR (status IN ('PROPOSED','CREATED','RETRY_PENDING')
            AND requested_configuration IS NOT NULL AND proposal_sha256 IS NOT NULL
            AND completed_at IS NULL)
        OR (status IN ('COMPLETED','CANDIDATE_FAILED','INFRASTRUCTURE_EXHAUSTED')
            AND requested_configuration IS NOT NULL AND proposal_sha256 IS NOT NULL
            AND completed_at IS NOT NULL)
    ),
    CHECK (
        (evaluation_role = 'DEFAULT_CONTROL' AND candidate_vector IS NULL)
        OR evaluation_role <> 'DEFAULT_CONTROL'
    )
);

CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_primary_attempts (
    primary_attempt_id uuid PRIMARY KEY,
    primary_run_id uuid NOT NULL
        REFERENCES charm_control.experiment_v2_primary_runs(primary_run_id),
    attempt_number integer NOT NULL CHECK (attempt_number BETWEEN 1 AND 3),
    trial_id uuid NOT NULL UNIQUE REFERENCES charm_control.trials(trial_id),
    status text NOT NULL CHECK (status IN (
        'CREATED','COMPLETED','CANDIDATE_FAILED','INFRASTRUCTURE_FAILED'
    )),
    failure_type text,
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    UNIQUE (primary_run_id,attempt_number),
    CHECK (
        (status = 'CREATED' AND completed_at IS NULL)
        OR (status <> 'CREATED' AND completed_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_primary_training_lineage (
    primary_run_id uuid NOT NULL
        REFERENCES charm_control.experiment_v2_primary_runs(primary_run_id),
    training_run_id uuid NOT NULL
        REFERENCES charm_control.experiment_v2_primary_runs(primary_run_id),
    training_position integer NOT NULL CHECK (training_position > 0),
    PRIMARY KEY (primary_run_id,training_run_id),
    UNIQUE (primary_run_id,training_position),
    CHECK (primary_run_id <> training_run_id)
);

CREATE INDEX IF NOT EXISTS v2_primary_runs_block_position_idx
    ON charm_control.experiment_v2_primary_runs(primary_block_id,global_position);
CREATE INDEX IF NOT EXISTS v2_primary_attempts_run_number_idx
    ON charm_control.experiment_v2_primary_attempts(primary_run_id,attempt_number);

CREATE OR REPLACE FUNCTION charm_control.validate_v2_primary_block_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'PLANNED' AND NEW.status IN ('RUNNING','FAILED'))
        OR (OLD.status = 'RUNNING' AND NEW.status IN (
            'PAUSED_INFRASTRUCTURE','OBSERVATIONS_COMPLETE','FAILED'
        ))
        OR (OLD.status = 'PAUSED_INFRASTRUCTURE' AND NEW.status = 'FAILED')
        OR (OLD.status = 'OBSERVATIONS_COMPLETE' AND NEW.status IN ('ANALYZED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid v2 primary block transition % -> %', OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('ANALYZED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'finalized v2 primary block is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION charm_control.validate_v2_primary_run_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF (
        NEW.global_position,NEW.seed_index,NEW.seed,NEW.within_seed_position,
        NEW.evaluation_role,NEW.method,NEW.budget_position,NEW.shared_with_methods,
        NEW.random_seed
    ) IS DISTINCT FROM (
        OLD.global_position,OLD.seed_index,OLD.seed,OLD.within_seed_position,
        OLD.evaluation_role,OLD.method,OLD.budget_position,OLD.shared_with_methods,
        OLD.random_seed
    ) THEN
        RAISE EXCEPTION 'v2 primary schedule identity is immutable';
    END IF;
    IF OLD.requested_configuration IS NOT NULL AND (
        NEW.candidate_vector,NEW.requested_configuration,NEW.proposal_sha256,
        NEW.acquisition_name,NEW.acquisition_value
    ) IS DISTINCT FROM (
        OLD.candidate_vector,OLD.requested_configuration,OLD.proposal_sha256,
        OLD.acquisition_name,OLD.acquisition_value
    ) THEN
        RAISE EXCEPTION 'materialized v2 primary proposal is immutable';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'PLANNED' AND NEW.status = 'PROPOSED')
        OR (OLD.status IN ('PROPOSED','RETRY_PENDING') AND NEW.status = 'CREATED')
        OR (OLD.status = 'CREATED' AND NEW.status IN (
            'COMPLETED','CANDIDATE_FAILED','RETRY_PENDING','INFRASTRUCTURE_EXHAUSTED'
        ))
    ) THEN
        RAISE EXCEPTION 'invalid v2 primary run transition % -> %', OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('COMPLETED','CANDIDATE_FAILED','INFRASTRUCTURE_EXHAUSTED')
       AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal v2 primary run is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION charm_control.validate_v2_primary_attempt_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.primary_run_id IS DISTINCT FROM OLD.primary_run_id
       OR NEW.attempt_number IS DISTINCT FROM OLD.attempt_number
       OR NEW.trial_id IS DISTINCT FROM OLD.trial_id THEN
        RAISE EXCEPTION 'v2 primary attempt identity is immutable';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        OLD.status = 'CREATED' AND NEW.status IN (
            'COMPLETED','CANDIDATE_FAILED','INFRASTRUCTURE_FAILED'
        )
    ) THEN
        RAISE EXCEPTION 'invalid v2 primary attempt transition % -> %', OLD.status, NEW.status;
    END IF;
    IF OLD.status <> 'CREATED' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal v2 primary attempt is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION charm_control.validate_v2_primary_training_lineage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    proposed charm_control.experiment_v2_primary_runs%ROWTYPE;
    training charm_control.experiment_v2_primary_runs%ROWTYPE;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'v2 primary training lineage is append-only';
    END IF;
    SELECT * INTO proposed FROM charm_control.experiment_v2_primary_runs
    WHERE primary_run_id = NEW.primary_run_id;
    SELECT * INTO training FROM charm_control.experiment_v2_primary_runs
    WHERE primary_run_id = NEW.training_run_id;
    IF proposed.method NOT IN (
        'bo_qlognei_throughput','bo_qlognparego_multiobjective',
        'bo_qlognehvi_multiobjective'
    ) OR proposed.seed <> training.seed
       OR training.global_position >= proposed.global_position
       OR training.status <> 'COMPLETED'
       OR NOT (training.method = 'bo_shared_initial' OR training.method = proposed.method) THEN
        RAISE EXCEPTION 'invalid or cross-method v2 primary training lineage';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS v2_primary_block_transition_guard
    ON charm_control.experiment_v2_primary_blocks;
CREATE TRIGGER v2_primary_block_transition_guard
BEFORE UPDATE ON charm_control.experiment_v2_primary_blocks
FOR EACH ROW EXECUTE FUNCTION charm_control.validate_v2_primary_block_transition();

DROP TRIGGER IF EXISTS v2_primary_run_transition_guard
    ON charm_control.experiment_v2_primary_runs;
CREATE TRIGGER v2_primary_run_transition_guard
BEFORE UPDATE ON charm_control.experiment_v2_primary_runs
FOR EACH ROW EXECUTE FUNCTION charm_control.validate_v2_primary_run_transition();

DROP TRIGGER IF EXISTS v2_primary_attempt_transition_guard
    ON charm_control.experiment_v2_primary_attempts;
CREATE TRIGGER v2_primary_attempt_transition_guard
BEFORE UPDATE ON charm_control.experiment_v2_primary_attempts
FOR EACH ROW EXECUTE FUNCTION charm_control.validate_v2_primary_attempt_transition();

DROP TRIGGER IF EXISTS v2_primary_training_lineage_guard
    ON charm_control.experiment_v2_primary_training_lineage;
CREATE TRIGGER v2_primary_training_lineage_guard
BEFORE INSERT OR UPDATE OR DELETE ON charm_control.experiment_v2_primary_training_lineage
FOR EACH ROW EXECUTE FUNCTION charm_control.validate_v2_primary_training_lineage();
