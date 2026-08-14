CREATE TABLE IF NOT EXISTS charm_control.experiment_warmup_duration_pilots (
    pilot_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL UNIQUE REFERENCES charm_control.campaigns(campaign_id),
    protocol_id text NOT NULL CHECK (protocol_id = 'thesis-protocol-v2'),
    evidence_role text NOT NULL CHECK (evidence_role = 'CALIBRATION'),
    manifest_sha256 text NOT NULL CHECK (length(manifest_sha256) = 64),
    phase2_manifest_sha256 text NOT NULL CHECK (length(phase2_manifest_sha256) = 64),
    preflight_id uuid NOT NULL
        REFERENCES charm_control.experiment_manifest_preflights(preflight_id),
    baseline_id uuid NOT NULL
        REFERENCES charm_control.experiment_candidate_dataset_baselines(baseline_id),
    status text NOT NULL CHECK (status IN (
        'WARMUP_PLANNED','WARMUP_RUNNING','WARMUP_COMPLETE','WARMUP_FROZEN',
        'DURATION_RUNNING','COMPLETED','FAILED'
    )),
    selected_warmup_seconds integer CHECK (
        selected_warmup_seconds IS NULL OR selected_warmup_seconds BETWEEN 0 AND 1200
    ),
    warmup_analysis jsonb,
    warmup_analysis_sha256 text CHECK (
        warmup_analysis_sha256 IS NULL OR length(warmup_analysis_sha256) = 64
    ),
    warmup_decision_reason text,
    duration_analysis jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    warmup_frozen_at timestamptz,
    completed_at timestamptz,
    CHECK (
        (status IN ('WARMUP_PLANNED','WARMUP_RUNNING','WARMUP_COMPLETE')
            AND selected_warmup_seconds IS NULL AND warmup_frozen_at IS NULL)
        OR (status IN ('WARMUP_FROZEN','DURATION_RUNNING','COMPLETED')
            AND selected_warmup_seconds IS NOT NULL
            AND warmup_analysis IS NOT NULL
            AND warmup_analysis_sha256 IS NOT NULL
            AND warmup_decision_reason <> ''
            AND warmup_frozen_at IS NOT NULL)
        OR status = 'FAILED'
    ),
    CHECK (
        (status = 'COMPLETED' AND completed_at IS NOT NULL)
        OR (status <> 'COMPLETED' AND completed_at IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS charm_control.experiment_warmup_duration_runs (
    run_id uuid PRIMARY KEY,
    pilot_id uuid NOT NULL
        REFERENCES charm_control.experiment_warmup_duration_pilots(pilot_id),
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    sequence integer NOT NULL CHECK (sequence BETWEEN 1 AND 25),
    subphase text NOT NULL CHECK (subphase IN (
        'WARMUP','DURATION_LONG','DURATION_SHORT'
    )),
    block integer CHECK (block IS NULL OR block BETWEEN 1 AND 3),
    configuration_name text NOT NULL CHECK (configuration_name <> ''),
    requested_configuration jsonb NOT NULL CHECK (
        jsonb_typeof(requested_configuration) = 'object'
    ),
    warmup_seconds integer NOT NULL CHECK (warmup_seconds BETWEEN 0 AND 1200),
    measurement_seconds integer NOT NULL CHECK (measurement_seconds BETWEEN 1 AND 1200),
    random_seed bigint NOT NULL,
    trial_id uuid UNIQUE REFERENCES charm_control.trials(trial_id),
    status text NOT NULL CHECK (status IN ('PLANNED','CREATED','COMPLETED','FAILED')),
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    UNIQUE (pilot_id,sequence),
    CHECK (
        (subphase = 'WARMUP' AND block IS NULL)
        OR (subphase IN ('DURATION_LONG','DURATION_SHORT') AND block IS NOT NULL)
    ),
    CHECK (
        (status = 'PLANNED' AND trial_id IS NULL AND completed_at IS NULL)
        OR (status = 'CREATED' AND trial_id IS NOT NULL AND completed_at IS NULL)
        OR (status IN ('COMPLETED','FAILED')
            AND trial_id IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS warmup_duration_runs_pilot_sequence_idx
    ON charm_control.experiment_warmup_duration_runs(pilot_id,sequence);

CREATE OR REPLACE FUNCTION charm_control.validate_warmup_duration_pilot_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'WARMUP_PLANNED' AND NEW.status IN ('WARMUP_RUNNING','FAILED'))
        OR (OLD.status = 'WARMUP_RUNNING' AND NEW.status IN ('WARMUP_COMPLETE','FAILED'))
        OR (OLD.status = 'WARMUP_COMPLETE' AND NEW.status IN ('WARMUP_FROZEN','FAILED'))
        OR (OLD.status = 'WARMUP_FROZEN' AND NEW.status IN ('DURATION_RUNNING','FAILED'))
        OR (OLD.status = 'DURATION_RUNNING' AND NEW.status IN ('COMPLETED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid warmup/duration pilot transition % -> %',
            OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS warmup_duration_pilot_transition_guard
    ON charm_control.experiment_warmup_duration_pilots;

CREATE TRIGGER warmup_duration_pilot_transition_guard
BEFORE UPDATE ON charm_control.experiment_warmup_duration_pilots
FOR EACH ROW
EXECUTE FUNCTION charm_control.validate_warmup_duration_pilot_transition();

CREATE OR REPLACE FUNCTION charm_control.validate_warmup_duration_run_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'PLANNED' AND NEW.status = 'CREATED')
        OR (OLD.status = 'CREATED' AND NEW.status IN ('COMPLETED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid warmup/duration run transition % -> %',
            OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('COMPLETED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'finalized warmup/duration run is immutable';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS warmup_duration_run_transition_guard
    ON charm_control.experiment_warmup_duration_runs;

CREATE TRIGGER warmup_duration_run_transition_guard
BEFORE UPDATE ON charm_control.experiment_warmup_duration_runs
FOR EACH ROW
EXECUTE FUNCTION charm_control.validate_warmup_duration_run_transition();
