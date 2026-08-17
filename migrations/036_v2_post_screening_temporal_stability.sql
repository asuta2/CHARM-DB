ALTER TABLE charm_control.experiment_v2_default_reference_blocks
    ADD COLUMN IF NOT EXISTS source_screening_recovery_block_id uuid
        REFERENCES charm_control.experiment_v2_screening_recovery_blocks(recovery_block_id),
    ADD COLUMN IF NOT EXISTS qualification_purpose text;

ALTER TABLE charm_control.experiment_v2_default_reference_blocks
    DROP CONSTRAINT IF EXISTS v2_default_reference_qualification_pair;
ALTER TABLE charm_control.experiment_v2_default_reference_blocks
    ADD CONSTRAINT v2_default_reference_qualification_pair CHECK (
        (source_screening_recovery_block_id IS NULL AND qualification_purpose IS NULL)
        OR (source_screening_recovery_block_id IS NOT NULL
            AND qualification_purpose = 'POST_SCREENING_TEMPORAL_STABILITY')
    );

CREATE UNIQUE INDEX IF NOT EXISTS v2_default_reference_temporal_source_unique
    ON charm_control.experiment_v2_default_reference_blocks(
        source_screening_recovery_block_id
    ) WHERE source_screening_recovery_block_id IS NOT NULL;
