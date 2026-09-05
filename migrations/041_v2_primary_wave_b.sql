ALTER TABLE charm_control.experiment_v2_primary_blocks
    ADD COLUMN IF NOT EXISTS wave text NOT NULL DEFAULT 'A';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'charm_control.experiment_v2_primary_blocks'::regclass
          AND conname = 'experiment_v2_primary_blocks_wave_check'
    ) THEN
        ALTER TABLE charm_control.experiment_v2_primary_blocks
            ADD CONSTRAINT experiment_v2_primary_blocks_wave_check
            CHECK (wave IN ('A', 'B'));
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS v2_primary_one_wave_b_block_idx
    ON charm_control.experiment_v2_primary_blocks (wave)
    WHERE wave = 'B';

CREATE OR REPLACE FUNCTION charm_control.validate_v2_primary_block_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF (
        NEW.campaign_id,NEW.protocol_id,NEW.evidence_role,NEW.manifest_sha256,
        NEW.preflight_id,NEW.baseline_id,NEW.benchmark_profile_id,
        NEW.schedule_sha256,NEW.candidate_design_sha256,NEW.wave,
        NEW.retry_policy,NEW.drift_interpretation
    ) IS DISTINCT FROM (
        OLD.campaign_id,OLD.protocol_id,OLD.evidence_role,OLD.manifest_sha256,
        OLD.preflight_id,OLD.baseline_id,OLD.benchmark_profile_id,
        OLD.schedule_sha256,OLD.candidate_design_sha256,OLD.wave,
        OLD.retry_policy,OLD.drift_interpretation
    ) THEN
        RAISE EXCEPTION 'v2 primary block protocol identity is immutable';
    END IF;
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
