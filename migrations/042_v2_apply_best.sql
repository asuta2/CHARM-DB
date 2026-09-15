CREATE TABLE IF NOT EXISTS charm_control.experiment_v2_apply_best_deployments (
    deployment_id uuid PRIMARY KEY,
    protocol_id text NOT NULL CHECK (protocol_id='thesis-protocol-v2'),
    evidence_role text NOT NULL CHECK (evidence_role='DEPLOYMENT_CONTROL'),
    manifest_sha256 text NOT NULL CHECK (length(manifest_sha256)=64),
    champion_treatment text NOT NULL CHECK (champion_treatment='E'),
    source_primary_run_id uuid NOT NULL,
    f4_campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    f4_analysis_sha256 text NOT NULL CHECK (length(f4_analysis_sha256)=64),
    wave_b_campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    wave_b_analysis_sha256 text NOT NULL CHECK (length(wave_b_analysis_sha256)=64),
    final_analysis_sha256 text NOT NULL CHECK (length(final_analysis_sha256)=64),
    configuration_sha256 text NOT NULL CHECK (length(configuration_sha256)=64),
    requested_configuration jsonb NOT NULL CHECK (jsonb_typeof(requested_configuration)='object'),
    status text NOT NULL CHECK (status IN (
        'PREPARED','RECOVERY_TESTING','RECOVERY_TESTED','ACTIVATING','ACTIVE',
        'ROLLING_BACK','ROLLED_BACK','FAILED'
    )),
    recovery_application_id uuid REFERENCES charm_control.configuration_applications(application_id),
    recovery_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    recovery_started_at timestamptz,
    recovery_completed_at timestamptz,
    activation_application_id uuid REFERENCES charm_control.configuration_applications(application_id),
    authorization_decision_id text,
    authorization_actor text,
    authorization_statement text,
    authorization_recorded_at timestamptz,
    activation_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    activated_at timestamptz,
    rolled_back_at timestamptz,
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (authorization_decision_id IS NULL AND authorization_actor IS NULL
         AND authorization_statement IS NULL AND authorization_recorded_at IS NULL)
        OR
        (authorization_decision_id IS NOT NULL AND authorization_decision_id<>''
         AND authorization_actor IS NOT NULL AND authorization_actor<>''
         AND authorization_statement IS NOT NULL AND authorization_statement<>''
         AND authorization_recorded_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS v2_apply_best_single_deployment_idx
    ON charm_control.experiment_v2_apply_best_deployments ((true));

CREATE OR REPLACE FUNCTION charm_control.validate_v2_apply_best_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF (
        NEW.protocol_id,NEW.evidence_role,NEW.manifest_sha256,NEW.champion_treatment,
        NEW.source_primary_run_id,NEW.f4_campaign_id,NEW.f4_analysis_sha256,
        NEW.wave_b_campaign_id,NEW.wave_b_analysis_sha256,NEW.final_analysis_sha256,
        NEW.configuration_sha256,NEW.requested_configuration,NEW.created_at
    ) IS DISTINCT FROM (
        OLD.protocol_id,OLD.evidence_role,OLD.manifest_sha256,OLD.champion_treatment,
        OLD.source_primary_run_id,OLD.f4_campaign_id,OLD.f4_analysis_sha256,
        OLD.wave_b_campaign_id,OLD.wave_b_analysis_sha256,OLD.final_analysis_sha256,
        OLD.configuration_sha256,OLD.requested_configuration,OLD.created_at
    ) THEN
        RAISE EXCEPTION 'v2 apply-best deployment identity is immutable';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status='PREPARED' AND NEW.status IN ('RECOVERY_TESTING','FAILED')) OR
        (OLD.status='RECOVERY_TESTING' AND NEW.status IN ('RECOVERY_TESTED','FAILED')) OR
        (OLD.status='RECOVERY_TESTED' AND NEW.status IN ('ACTIVATING','FAILED')) OR
        (OLD.status='ACTIVATING' AND NEW.status IN ('ACTIVE','FAILED')) OR
        (OLD.status='ACTIVE' AND NEW.status IN ('ROLLING_BACK','FAILED')) OR
        (OLD.status='ROLLING_BACK' AND NEW.status IN ('ROLLED_BACK','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid v2 apply-best transition % -> %', OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('ROLLED_BACK','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal v2 apply-best deployment is immutable';
    END IF;
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS validate_v2_apply_best_transition
    ON charm_control.experiment_v2_apply_best_deployments;
CREATE TRIGGER validate_v2_apply_best_transition
BEFORE UPDATE ON charm_control.experiment_v2_apply_best_deployments
FOR EACH ROW EXECUTE FUNCTION charm_control.validate_v2_apply_best_transition();
