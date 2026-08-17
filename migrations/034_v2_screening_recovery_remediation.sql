ALTER TABLE charm_control.experiment_v2_restore_stability_blocks
    DROP CONSTRAINT IF EXISTS experiment_v2_restore_stability_blocks_source_screening_block_id_key;

CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_target_database_remediations (
    remediation_id uuid PRIMARY KEY,
    source_screening_block_id uuid NOT NULL
        REFERENCES charm_control.experiment_v2_screening_blocks(block_id),
    failed_validation_block_id uuid NOT NULL UNIQUE
        REFERENCES charm_control.experiment_v2_restore_stability_blocks(validation_block_id),
    protocol_id text NOT NULL CHECK (protocol_id = 'thesis-protocol-v2'),
    evidence_role text NOT NULL CHECK (evidence_role = 'INFRASTRUCTURE'),
    manifest_sha256 text NOT NULL UNIQUE CHECK (length(manifest_sha256) = 64),
    remediation_sha256 text NOT NULL CHECK (length(remediation_sha256) = 64),
    status text NOT NULL CHECK (status IN ('PLANNED','RUNNING','PASSED','FAILED')),
    pre_evidence jsonb NOT NULL CHECK (jsonb_typeof(pre_evidence) = 'object'),
    post_evidence jsonb,
    result jsonb,
    result_sha256 text CHECK (result_sha256 IS NULL OR length(result_sha256) = 64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    completed_at timestamptz,
    CHECK (
        (status = 'PLANNED' AND started_at IS NULL AND completed_at IS NULL)
        OR (status = 'RUNNING' AND started_at IS NOT NULL AND completed_at IS NULL)
        OR (status IN ('PASSED','FAILED')
            AND started_at IS NOT NULL AND completed_at IS NOT NULL)
    )
);

ALTER TABLE charm_control.experiment_v2_restore_stability_blocks
    ADD COLUMN IF NOT EXISTS remediation_id uuid
        REFERENCES charm_control.experiment_v2_target_database_remediations(remediation_id),
    ADD COLUMN IF NOT EXISTS supersedes_validation_block_id uuid
        REFERENCES charm_control.experiment_v2_restore_stability_blocks(validation_block_id);

CREATE UNIQUE INDEX IF NOT EXISTS v2_restore_stability_source_manifest_unique
    ON charm_control.experiment_v2_restore_stability_blocks(
        source_screening_block_id,manifest_sha256
    );

CREATE OR REPLACE FUNCTION charm_control.validate_v2_target_database_remediation_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'PLANNED' AND NEW.status IN ('RUNNING','FAILED'))
        OR (OLD.status = 'RUNNING' AND NEW.status IN ('PASSED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid v2 target-database remediation transition % -> %',
            OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('PASSED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'finalized v2 target-database remediation is immutable';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS v2_target_database_remediation_transition_guard
    ON charm_control.experiment_v2_target_database_remediations;
CREATE TRIGGER v2_target_database_remediation_transition_guard
BEFORE UPDATE ON charm_control.experiment_v2_target_database_remediations
FOR EACH ROW
EXECUTE FUNCTION charm_control.validate_v2_target_database_remediation_transition();
