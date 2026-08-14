ALTER TABLE charm_control.trials
    ADD COLUMN IF NOT EXISTS protocol_id text,
    ADD COLUMN IF NOT EXISTS evidence_role text,
    ADD COLUMN IF NOT EXISTS evaluation_role text,
    ADD COLUMN IF NOT EXISTS candidate_restore_required boolean NOT NULL DEFAULT false;

ALTER TABLE charm_control.trials
    DROP CONSTRAINT IF EXISTS trials_evidence_role_valid,
    DROP CONSTRAINT IF EXISTS trials_v2_metadata_complete;

ALTER TABLE charm_control.trials
    ADD CONSTRAINT trials_evidence_role_valid CHECK (
        evidence_role IS NULL OR evidence_role IN (
            'INFRASTRUCTURE',
            'CALIBRATION',
            'HISTORICAL_DIAGNOSTIC',
            'PRIMARY',
            'SECONDARY',
            'F4_CONFIRMATION'
        )
    ),
    ADD CONSTRAINT trials_v2_metadata_complete CHECK (
        (protocol_id IS NULL AND evidence_role IS NULL AND NOT candidate_restore_required)
        OR (protocol_id = 'thesis-protocol-v2' AND evidence_role IS NOT NULL)
    );

CREATE TABLE IF NOT EXISTS charm_control.experiment_candidate_dataset_baselines (
    baseline_id uuid PRIMARY KEY,
    preflight_id uuid NOT NULL UNIQUE
        REFERENCES charm_control.experiment_manifest_preflights(preflight_id),
    validation_id uuid NOT NULL UNIQUE
        REFERENCES charm_control.experiment_dataset_restore_validations(validation_id),
    restore_mechanism text NOT NULL,
    exact_core jsonb NOT NULL,
    physical_statistics jsonb NOT NULL,
    physical_tolerances jsonb NOT NULL,
    approved boolean NOT NULL DEFAULT false,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    approved_at timestamptz,
    CHECK (restore_mechanism IN ('copy-on-write','physical-archive','logical-restore')),
    CHECK ((approved AND approved_at IS NOT NULL) OR (NOT approved AND approved_at IS NULL))
);

CREATE TABLE IF NOT EXISTS charm_control.experiment_candidate_dataset_restores (
    restore_id uuid PRIMARY KEY,
    trial_id uuid NOT NULL REFERENCES charm_control.trials(trial_id),
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    experiment_arm_id uuid REFERENCES charm_control.experiment_arms(arm_id),
    budget_position integer CHECK (budget_position IS NULL OR budget_position >= 0),
    protocol_id text NOT NULL CHECK (protocol_id = 'thesis-protocol-v2'),
    evidence_role text NOT NULL CHECK (evidence_role IN (
        'INFRASTRUCTURE',
        'CALIBRATION',
        'PRIMARY',
        'SECONDARY',
        'F4_CONFIRMATION'
    )),
    baseline_id uuid NOT NULL
        REFERENCES charm_control.experiment_candidate_dataset_baselines(baseline_id),
    preflight_id uuid NOT NULL
        REFERENCES charm_control.experiment_manifest_preflights(preflight_id),
    validation_id uuid
        REFERENCES charm_control.experiment_dataset_restore_validations(validation_id),
    restore_mechanism text NOT NULL,
    restore_source text NOT NULL,
    snapshot_sha256 text NOT NULL CHECK (length(snapshot_sha256) = 64),
    image_digest text NOT NULL,
    status text NOT NULL,
    attempt integer NOT NULL CHECK (attempt >= 1),
    idempotency_key text NOT NULL,
    retry_of_restore_id uuid
        REFERENCES charm_control.experiment_candidate_dataset_restores(restore_id),
    recovery_root_id uuid NOT NULL
        REFERENCES charm_control.experiment_candidate_dataset_restores(restore_id),
    started_at timestamptz,
    completed_at timestamptz,
    duration_seconds double precision CHECK (
        duration_seconds IS NULL OR duration_seconds >= 0
    ),
    pre_restore_fingerprint jsonb,
    post_restore_fingerprint jsonb,
    exact_core_passed boolean,
    physical_statistics_passed boolean,
    verification_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    failure_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (trial_id, attempt),
    UNIQUE (idempotency_key, attempt),
    CHECK (restore_mechanism IN ('copy-on-write','physical-archive','logical-restore')),
    CHECK (status IN (
        'REQUESTED','RESTORING','VERIFYING','PASSED','FAILED','INTERRUPTED'
    )),
    CHECK (
        (status = 'REQUESTED' AND started_at IS NULL AND completed_at IS NULL)
        OR (status IN ('RESTORING','VERIFYING') AND started_at IS NOT NULL
            AND completed_at IS NULL)
        OR (status IN ('PASSED','FAILED','INTERRUPTED') AND started_at IS NOT NULL
            AND completed_at IS NOT NULL AND duration_seconds IS NOT NULL)
    ),
    CHECK (
        status <> 'PASSED'
        OR (
            validation_id IS NOT NULL
            AND pre_restore_fingerprint IS NOT NULL
            AND post_restore_fingerprint IS NOT NULL
            AND exact_core_passed IS TRUE
            AND physical_statistics_passed IS TRUE
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS candidate_dataset_restores_passed_trial_idx
    ON charm_control.experiment_candidate_dataset_restores(trial_id)
    WHERE status = 'PASSED';

CREATE INDEX IF NOT EXISTS candidate_dataset_restores_trial_attempt_idx
    ON charm_control.experiment_candidate_dataset_restores(trial_id,attempt DESC);

CREATE INDEX IF NOT EXISTS candidate_dataset_restores_preflight_idx
    ON charm_control.experiment_candidate_dataset_restores(preflight_id,created_at DESC);

ALTER TABLE charm_control.trials
    ADD COLUMN IF NOT EXISTS candidate_dataset_restore_id uuid
        REFERENCES charm_control.experiment_candidate_dataset_restores(restore_id);

ALTER TABLE charm_control.trials
    DROP CONSTRAINT IF EXISTS trials_candidate_restore_link_valid;

ALTER TABLE charm_control.trials
    ADD CONSTRAINT trials_candidate_restore_link_valid CHECK (
        candidate_dataset_restore_id IS NULL OR candidate_restore_required
    );

CREATE OR REPLACE FUNCTION charm_control.validate_candidate_dataset_restore_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT (
            (OLD.status = 'REQUESTED' AND NEW.status IN ('RESTORING','FAILED'))
            OR (OLD.status = 'RESTORING'
                AND NEW.status IN ('VERIFYING','FAILED','INTERRUPTED'))
            OR (OLD.status = 'VERIFYING'
                AND NEW.status IN ('PASSED','FAILED','INTERRUPTED'))
        ) THEN
            RAISE EXCEPTION 'invalid candidate restore transition % -> %',
                OLD.status, NEW.status;
        END IF;
    END IF;
    IF OLD.status IN ('PASSED','FAILED','INTERRUPTED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'finalized candidate restore evidence is immutable';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS candidate_dataset_restore_transition_guard
    ON charm_control.experiment_candidate_dataset_restores;

CREATE TRIGGER candidate_dataset_restore_transition_guard
BEFORE UPDATE ON charm_control.experiment_candidate_dataset_restores
FOR EACH ROW
EXECUTE FUNCTION charm_control.validate_candidate_dataset_restore_transition();

CREATE OR REPLACE FUNCTION charm_control.prevent_candidate_baseline_rewrite()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.approved THEN
        RAISE EXCEPTION 'approved candidate dataset baseline is immutable';
    END IF;
    IF NEW.preflight_id IS DISTINCT FROM OLD.preflight_id
       OR NEW.validation_id IS DISTINCT FROM OLD.validation_id
       OR NEW.restore_mechanism IS DISTINCT FROM OLD.restore_mechanism
       OR NEW.exact_core IS DISTINCT FROM OLD.exact_core
       OR NEW.physical_statistics IS DISTINCT FROM OLD.physical_statistics
       OR NEW.physical_tolerances IS DISTINCT FROM OLD.physical_tolerances
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'candidate dataset baseline evidence is immutable';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS candidate_dataset_baseline_immutable
    ON charm_control.experiment_candidate_dataset_baselines;

CREATE TRIGGER candidate_dataset_baseline_immutable
BEFORE UPDATE ON charm_control.experiment_candidate_dataset_baselines
FOR EACH ROW
EXECUTE FUNCTION charm_control.prevent_candidate_baseline_rewrite();
