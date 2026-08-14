CREATE TABLE IF NOT EXISTS charm_control.experiment_saturation_phase2_configurations (
    configuration_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    sequence integer NOT NULL CHECK (sequence BETWEEN 1 AND 8),
    protocol_id text NOT NULL CHECK (protocol_id = 'thesis-protocol-v2'),
    evidence_role text NOT NULL CHECK (evidence_role = 'CALIBRATION'),
    manifest_sha256 text NOT NULL CHECK (length(manifest_sha256) = 64),
    configuration_name text NOT NULL,
    requested_configuration jsonb NOT NULL,
    random_seed bigint NOT NULL,
    trial_id uuid UNIQUE REFERENCES charm_control.trials(trial_id),
    status text NOT NULL CHECK (status IN ('PLANNED','CREATED','COMPLETED','FAILED')),
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    UNIQUE (campaign_id,sequence),
    UNIQUE (campaign_id,configuration_name),
    CHECK (jsonb_typeof(requested_configuration) = 'object'),
    CHECK (configuration_name <> ''),
    CHECK (
        (status = 'PLANNED' AND trial_id IS NULL AND completed_at IS NULL)
        OR (status = 'CREATED' AND trial_id IS NOT NULL AND completed_at IS NULL)
        OR (status IN ('COMPLETED','FAILED')
            AND trial_id IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS saturation_phase2_campaign_sequence_idx
    ON charm_control.experiment_saturation_phase2_configurations(campaign_id,sequence);

CREATE OR REPLACE FUNCTION charm_control.validate_saturation_phase2_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'PLANNED' AND NEW.status = 'CREATED')
        OR (OLD.status = 'CREATED' AND NEW.status IN ('COMPLETED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid saturation Phase 2 transition % -> %',
            OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('COMPLETED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'finalized saturation Phase 2 configuration is immutable';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS saturation_phase2_transition_guard
    ON charm_control.experiment_saturation_phase2_configurations;

CREATE TRIGGER saturation_phase2_transition_guard
BEFORE UPDATE ON charm_control.experiment_saturation_phase2_configurations
FOR EACH ROW
EXECUTE FUNCTION charm_control.validate_saturation_phase2_transition();
