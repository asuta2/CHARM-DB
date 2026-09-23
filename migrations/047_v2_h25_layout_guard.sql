ALTER TABLE charm_control.experiment_v2_h25_runs
    DROP CONSTRAINT IF EXISTS v2_h25_runs_default_at_center,
    DROP CONSTRAINT IF EXISTS v2_h25_runs_treatments_at_outer_positions;

CREATE OR REPLACE FUNCTION charm_control.validate_v2_h25_run_layout()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    block_design text;
BEGIN
    SELECT design_kind INTO block_design
    FROM charm_control.experiment_v2_h25_blocks WHERE h25_block_id=NEW.h25_block_id;
    IF block_design='three-arm-heuristic-default-champion' THEN
        IF NEW.physical_position<>(NEW.repetition_block-1)*3+NEW.within_block_position THEN
            RAISE EXCEPTION 'v2 H25 three-arm physical position % is inconsistent with block % slot %',
                NEW.physical_position,NEW.repetition_block,NEW.within_block_position;
        END IF;
        IF NEW.treatment='DEFAULT' AND NEW.within_block_position<>2 THEN
            RAISE EXCEPTION 'v2 H25 three-arm DEFAULT must occupy the center position';
        END IF;
        IF NEW.treatment<>'DEFAULT' AND NEW.within_block_position NOT IN (1,3) THEN
            RAISE EXCEPTION 'v2 H25 three-arm % must occupy an outer position',NEW.treatment;
        END IF;
    ELSIF NEW.treatment='E' THEN
        RAISE EXCEPTION 'v2 H25 two-arm blocks cannot hold the champion arm';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS v2_h25_run_layout_guard ON charm_control.experiment_v2_h25_runs;
CREATE TRIGGER v2_h25_run_layout_guard BEFORE INSERT ON charm_control.experiment_v2_h25_runs
FOR EACH ROW EXECUTE FUNCTION charm_control.validate_v2_h25_run_layout();
