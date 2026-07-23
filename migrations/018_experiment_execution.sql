ALTER TABLE charm_control.experiment_arms
    DROP CONSTRAINT IF EXISTS experiment_arms_status_check;

ALTER TABLE charm_control.experiment_arms
    ADD CONSTRAINT experiment_arms_status_check CHECK (
        status IN ('PLANNED','BLOCKED','PREPARED','RUNNING','COMPLETED','FAILED')
    ),
    ADD COLUMN IF NOT EXISTS execution_manifest jsonb,
    ADD COLUMN IF NOT EXISTS manifest_sha256 text,
    ADD COLUMN IF NOT EXISTS gate_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS realized_budget_value double precision NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS budget_breakdown jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS started_at timestamptz,
    ADD COLUMN IF NOT EXISTS failure_reason text;

ALTER TABLE charm_control.experiment_arms
    DROP CONSTRAINT IF EXISTS experiment_arms_manifest_complete,
    DROP CONSTRAINT IF EXISTS experiment_arms_realized_budget_nonnegative;

ALTER TABLE charm_control.experiment_arms
    ADD CONSTRAINT experiment_arms_manifest_complete CHECK (
        (execution_manifest IS NULL AND manifest_sha256 IS NULL)
        OR (execution_manifest IS NOT NULL AND manifest_sha256 IS NOT NULL)
    ),
    ADD CONSTRAINT experiment_arms_realized_budget_nonnegative CHECK (
        realized_budget_value >= 0
    );

CREATE UNIQUE INDEX IF NOT EXISTS experiment_arms_campaign_unique_idx
    ON charm_control.experiment_arms (campaign_id)
    WHERE campaign_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS charm_control.experiment_arm_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    arm_id uuid NOT NULL REFERENCES charm_control.experiment_arms(arm_id),
    event_type text NOT NULL,
    previous_status text,
    new_status text NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS charm_control.experiment_budget_entries (
    budget_entry_id uuid PRIMARY KEY,
    arm_id uuid NOT NULL REFERENCES charm_control.experiment_arms(arm_id),
    trial_id uuid NOT NULL REFERENCES charm_control.trials(trial_id),
    phase text NOT NULL,
    fidelity integer NOT NULL CHECK (fidelity BETWEEN 0 AND 4),
    wall_clock_seconds double precision NOT NULL CHECK (wall_clock_seconds >= 0),
    unit_value double precision NOT NULL CHECK (unit_value >= 0),
    accounting_kind text NOT NULL CHECK (
        accounting_kind IN ('F3_EQUIVALENT_WALL_CLOCK','DRIFT_PHASE_UNITS')
    ),
    operational_cost jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (arm_id, trial_id)
);

CREATE INDEX IF NOT EXISTS experiment_arm_events_time_idx
    ON charm_control.experiment_arm_events (arm_id, occurred_at);

CREATE INDEX IF NOT EXISTS experiment_budget_entries_arm_idx
    ON charm_control.experiment_budget_entries (arm_id, phase, created_at);

CREATE OR REPLACE FUNCTION charm_control.prevent_experiment_manifest_rewrite()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.execution_manifest IS NOT NULL AND (
        NEW.execution_manifest IS DISTINCT FROM OLD.execution_manifest
        OR NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256
    ) THEN
        RAISE EXCEPTION 'experiment execution manifest is immutable once frozen';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS experiment_arm_manifest_immutable
    ON charm_control.experiment_arms;

CREATE TRIGGER experiment_arm_manifest_immutable
BEFORE UPDATE ON charm_control.experiment_arms
FOR EACH ROW
EXECUTE FUNCTION charm_control.prevent_experiment_manifest_rewrite();
