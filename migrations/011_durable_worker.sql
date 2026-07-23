ALTER TABLE charm_control.campaigns
    ADD COLUMN IF NOT EXISTS settings jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS failure_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS failure_limit integer NOT NULL DEFAULT 5,
    ADD COLUMN IF NOT EXISTS emergency_stop boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS stopped_reason text;

ALTER TABLE charm_control.trials
    ADD COLUMN IF NOT EXISTS workflow_kind text NOT NULL DEFAULT 'RESEARCH',
    ADD COLUMN IF NOT EXISTS workflow_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS workflow_result jsonb,
    ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_attempts integer NOT NULL DEFAULT 3,
    ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ADD COLUMN IF NOT EXISTS lease_owner text,
    ADD COLUMN IF NOT EXISTS lease_token uuid,
    ADD COLUMN IF NOT EXISTS lease_acquired_at timestamptz,
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz,
    ADD COLUMN IF NOT EXISTS idempotency_key text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'campaigns_failure_count_nonnegative'
          AND conrelid = 'charm_control.campaigns'::regclass
    ) THEN
        ALTER TABLE charm_control.campaigns
            ADD CONSTRAINT campaigns_failure_count_nonnegative
            CHECK (failure_count >= 0 AND failure_limit >= 1);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'trials_attempt_count_valid'
          AND conrelid = 'charm_control.trials'::regclass
    ) THEN
        ALTER TABLE charm_control.trials
            ADD CONSTRAINT trials_attempt_count_valid
            CHECK (attempt_count >= 0 AND max_attempts >= 1);
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS trials_campaign_idempotency_idx
    ON charm_control.trials (campaign_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS trials_worker_claim_idx
    ON charm_control.trials (next_attempt_at, created_at)
    WHERE completed_at IS NULL;

CREATE TABLE IF NOT EXISTS charm_control.campaign_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    event_type text NOT NULL,
    previous_status text,
    new_status text NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS charm_control.trial_action_executions (
    execution_id uuid PRIMARY KEY,
    trial_id uuid NOT NULL REFERENCES charm_control.trials(trial_id),
    state text NOT NULL,
    idempotency_key text NOT NULL,
    status text NOT NULL,
    attempt integer NOT NULL CHECK (attempt >= 1),
    lease_token uuid NOT NULL,
    result jsonb,
    error jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    UNIQUE (trial_id, state, idempotency_key),
    CHECK (status IN ('STARTED','COMPLETED','FAILED'))
);

CREATE INDEX IF NOT EXISTS campaign_events_time_idx
    ON charm_control.campaign_events (campaign_id, occurred_at);
CREATE INDEX IF NOT EXISTS trial_action_status_idx
    ON charm_control.trial_action_executions (trial_id, status, started_at);
