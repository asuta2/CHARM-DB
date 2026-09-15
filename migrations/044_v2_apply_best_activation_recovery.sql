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
        (OLD.status='ROLLING_BACK' AND NEW.status IN ('ROLLED_BACK','FAILED')) OR
        (
            OLD.status='FAILED' AND NEW.status='RECOVERY_TESTING'
            AND OLD.authorization_recorded_at IS NULL
            AND NEW.recovery_application_id IS NOT NULL
            AND EXISTS (
                SELECT 1
                FROM charm_control.configuration_applications a
                JOIN charm_control.rollbacks r USING(application_id)
                WHERE a.application_id=NEW.recovery_application_id
                  AND a.status='ROLLED_BACK' AND r.verified
            )
        ) OR
        (
            OLD.status IN ('ACTIVATING','FAILED') AND NEW.status='ROLLING_BACK'
            AND OLD.authorization_recorded_at IS NOT NULL
            AND NEW.activation_application_id IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM charm_control.configuration_applications a
                WHERE a.application_id=NEW.activation_application_id
            )
        )
    ) THEN
        RAISE EXCEPTION 'invalid v2 apply-best transition % -> %', OLD.status, NEW.status;
    END IF;
    IF OLD.status='ROLLED_BACK' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal v2 apply-best deployment is immutable';
    END IF;
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END
$$;
