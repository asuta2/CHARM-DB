ALTER TABLE charm_control.experiment_v2_h25_blocks
    ADD COLUMN IF NOT EXISTS design_kind text NOT NULL
        DEFAULT 'two-arm-default-versus-heuristic',
    ADD COLUMN IF NOT EXISTS supersedes_h25_block_id uuid
        REFERENCES charm_control.experiment_v2_h25_blocks(h25_block_id);

ALTER TABLE charm_control.experiment_v2_h25_blocks
    DROP CONSTRAINT IF EXISTS v2_h25_blocks_design_kind_valid,
    ADD CONSTRAINT v2_h25_blocks_design_kind_valid CHECK (design_kind IN (
        'two-arm-default-versus-heuristic','three-arm-heuristic-default-champion'
    )),
    DROP CONSTRAINT IF EXISTS v2_h25_blocks_supersedes_other_block,
    ADD CONSTRAINT v2_h25_blocks_supersedes_other_block CHECK (
        supersedes_h25_block_id IS NULL OR supersedes_h25_block_id<>h25_block_id
    );

ALTER TABLE charm_control.experiment_v2_h25_runs
    ADD COLUMN IF NOT EXISTS source_primary_run_id uuid;

ALTER TABLE charm_control.experiment_v2_h25_runs
    DROP CONSTRAINT IF EXISTS experiment_v2_h25_runs_physical_position_check,
    DROP CONSTRAINT IF EXISTS experiment_v2_h25_runs_within_block_position_check,
    DROP CONSTRAINT IF EXISTS experiment_v2_h25_runs_treatment_check,
    DROP CONSTRAINT IF EXISTS v2_h25_runs_physical_position_range,
    ADD CONSTRAINT v2_h25_runs_physical_position_range
        CHECK (physical_position BETWEEN 1 AND 12),
    DROP CONSTRAINT IF EXISTS v2_h25_runs_within_block_position_range,
    ADD CONSTRAINT v2_h25_runs_within_block_position_range
        CHECK (within_block_position BETWEEN 1 AND 3),
    DROP CONSTRAINT IF EXISTS v2_h25_runs_treatment_valid,
    ADD CONSTRAINT v2_h25_runs_treatment_valid CHECK (treatment IN ('DEFAULT','H25','E')),
    DROP CONSTRAINT IF EXISTS v2_h25_runs_source_identity,
    ADD CONSTRAINT v2_h25_runs_source_identity CHECK (
        (treatment='E' AND source_primary_run_id='e6d70fab-0c90-5795-96a1-5021bff1194c'::uuid)
        OR (treatment<>'E' AND source_primary_run_id IS NULL)
    ),
    DROP CONSTRAINT IF EXISTS v2_h25_runs_champion_configuration,
    ADD CONSTRAINT v2_h25_runs_champion_configuration CHECK (
        treatment<>'E' OR requested_configuration='{
            "checkpoint_completion_target": "0.936696",
            "checkpoint_timeout": "1430",
            "effective_cache_size": "465506",
            "max_parallel_workers_per_gather": "3",
            "max_wal_size": "633",
            "random_page_cost": "3.655316",
            "shared_buffers": "182848",
            "work_mem": "18022"
        }'::jsonb
    ),
    DROP CONSTRAINT IF EXISTS v2_h25_runs_default_at_center,
    ADD CONSTRAINT v2_h25_runs_default_at_center
        CHECK (treatment<>'DEFAULT' OR within_block_position=2) NOT VALID,
    DROP CONSTRAINT IF EXISTS v2_h25_runs_treatments_at_outer_positions,
    ADD CONSTRAINT v2_h25_runs_treatments_at_outer_positions
        CHECK (treatment='DEFAULT' OR within_block_position IN (1,3)) NOT VALID;

CREATE OR REPLACE FUNCTION charm_control.validate_v2_h25_run_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.h25_block_id,NEW.campaign_id,NEW.physical_position,NEW.repetition_block,
        NEW.within_block_position,NEW.treatment,NEW.source_primary_run_id,NEW.random_seed,
        NEW.requested_configuration,NEW.proposal_sha256)
       IS DISTINCT FROM
       (OLD.h25_block_id,OLD.campaign_id,OLD.physical_position,OLD.repetition_block,
        OLD.within_block_position,OLD.treatment,OLD.source_primary_run_id,OLD.random_seed,
        OLD.requested_configuration,OLD.proposal_sha256) THEN
        RAISE EXCEPTION 'v2 H25 slot identity is immutable';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status IN ('PROPOSED','RETRY_PENDING') AND NEW.status='CREATED') OR
        (OLD.status='CREATED' AND NEW.status IN
            ('COMPLETED','CANDIDATE_FAILED','RETRY_PENDING','INFRASTRUCTURE_EXHAUSTED'))
    ) THEN
        RAISE EXCEPTION 'invalid v2 H25 run transition % -> %',OLD.status,NEW.status;
    END IF;
    IF OLD.status IN ('COMPLETED','CANDIDATE_FAILED','INFRASTRUCTURE_EXHAUSTED')
       AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal v2 H25 run is immutable';
    END IF;
    RETURN NEW;
END $$;
