CREATE TABLE IF NOT EXISTS charm_control.experiment_saturation_phase1_ladders (
    ladder_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    sequence integer NOT NULL CHECK (sequence BETWEEN 1 AND 9),
    protocol_id text NOT NULL CHECK (protocol_id = 'thesis-protocol-v2'),
    evidence_role text NOT NULL CHECK (evidence_role = 'CALIBRATION'),
    manifest_sha256 text NOT NULL CHECK (length(manifest_sha256) = 64),
    scale integer NOT NULL CHECK (scale IN (100,250,500)),
    anchor_name text NOT NULL CHECK (
        anchor_name IN ('postgresql_default','throughput_leaning','latency_leaning')
    ),
    warmup_seconds integer NOT NULL CHECK (warmup_seconds = 60),
    measurement_seconds integer NOT NULL CHECK (measurement_seconds = 120),
    client_thread_cap integer NOT NULL CHECK (client_thread_cap = 4),
    requested_configuration jsonb NOT NULL,
    verified_configuration jsonb,
    configuration_application_id uuid
        REFERENCES charm_control.configuration_applications(application_id),
    dataset_state jsonb,
    status text NOT NULL CHECK (
        status IN ('PLANNED','PREPARING','READY','RUNNING','COMPLETED','FAILED')
    ),
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    completed_at timestamptz,
    UNIQUE (campaign_id,sequence),
    UNIQUE (campaign_id,scale,anchor_name),
    CHECK (
        (status = 'PLANNED' AND started_at IS NULL AND completed_at IS NULL)
        OR (status IN ('PREPARING','READY','RUNNING')
            AND started_at IS NOT NULL AND completed_at IS NULL)
        OR (status IN ('COMPLETED','FAILED')
            AND started_at IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS charm_control.experiment_saturation_phase1_probes (
    probe_id uuid PRIMARY KEY,
    ladder_id uuid NOT NULL
        REFERENCES charm_control.experiment_saturation_phase1_ladders(ladder_id),
    sequence integer NOT NULL CHECK (sequence BETWEEN 1 AND 5),
    concurrency integer NOT NULL CHECK (concurrency IN (4,8,16,32,64)),
    client_threads integer NOT NULL CHECK (
        client_threads BETWEEN 1 AND 4 AND client_threads <= concurrency
    ),
    random_seed bigint NOT NULL,
    trial_id uuid UNIQUE REFERENCES charm_control.trials(trial_id),
    status text NOT NULL CHECK (status IN ('PLANNED','CREATED','COMPLETED','FAILED')),
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    UNIQUE (ladder_id,sequence),
    UNIQUE (ladder_id,concurrency),
    CHECK (
        (status = 'PLANNED' AND trial_id IS NULL AND completed_at IS NULL)
        OR (status = 'CREATED' AND trial_id IS NOT NULL AND completed_at IS NULL)
        OR (status IN ('COMPLETED','FAILED')
            AND trial_id IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS saturation_phase1_ladders_campaign_idx
    ON charm_control.experiment_saturation_phase1_ladders(campaign_id,sequence);

CREATE INDEX IF NOT EXISTS saturation_phase1_probes_ladder_idx
    ON charm_control.experiment_saturation_phase1_probes(ladder_id,sequence);

CREATE OR REPLACE FUNCTION charm_control.validate_saturation_phase1_ladder_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'PLANNED' AND NEW.status IN ('PREPARING','FAILED'))
        OR (OLD.status = 'PREPARING' AND NEW.status IN ('READY','FAILED'))
        OR (OLD.status = 'READY' AND NEW.status IN ('RUNNING','FAILED'))
        OR (OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid saturation ladder transition % -> %',
            OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('COMPLETED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'finalized saturation ladder is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION charm_control.validate_saturation_phase1_probe_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'PLANNED' AND NEW.status = 'CREATED')
        OR (OLD.status = 'CREATED' AND NEW.status IN ('COMPLETED','FAILED'))
    ) THEN
        RAISE EXCEPTION 'invalid saturation probe transition % -> %',
            OLD.status, NEW.status;
    END IF;
    IF OLD.status IN ('COMPLETED','FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'finalized saturation probe is immutable';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS saturation_phase1_ladder_transition_guard
    ON charm_control.experiment_saturation_phase1_ladders;

CREATE TRIGGER saturation_phase1_ladder_transition_guard
BEFORE UPDATE ON charm_control.experiment_saturation_phase1_ladders
FOR EACH ROW
EXECUTE FUNCTION charm_control.validate_saturation_phase1_ladder_transition();

DROP TRIGGER IF EXISTS saturation_phase1_probe_transition_guard
    ON charm_control.experiment_saturation_phase1_probes;

CREATE TRIGGER saturation_phase1_probe_transition_guard
BEFORE UPDATE ON charm_control.experiment_saturation_phase1_probes
FOR EACH ROW
EXECUTE FUNCTION charm_control.validate_saturation_phase1_probe_transition();
