CREATE TABLE IF NOT EXISTS charm_control.experiment_groups (
    group_id uuid PRIMARY KEY,
    group_key text NOT NULL UNIQUE,
    group_kind text NOT NULL CHECK (group_kind IN ('COMPARISON','ABLATION')),
    hypothesis text NOT NULL,
    baseline text NOT NULL,
    budget_unit text NOT NULL,
    budget_value double precision NOT NULL CHECK (budget_value > 0),
    seeds jsonb NOT NULL,
    required_metrics jsonb NOT NULL,
    prerequisite_gate text NOT NULL,
    status text NOT NULL CHECK (status IN ('PLANNED','ELIGIBLE','BLOCKED','RUNNING','COMPLETED','FAILED')),
    blocked_reason text,
    preregistered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS charm_control.experiment_arms (
    arm_id uuid PRIMARY KEY,
    group_id uuid NOT NULL REFERENCES charm_control.experiment_groups(group_id),
    label text NOT NULL,
    method_definition jsonb NOT NULL,
    random_seed bigint NOT NULL,
    block_order integer NOT NULL CHECK (block_order >= 0),
    budget_value double precision NOT NULL CHECK (budget_value > 0),
    campaign_id uuid REFERENCES charm_control.campaigns(campaign_id),
    status text NOT NULL CHECK (status IN ('PLANNED','BLOCKED','RUNNING','COMPLETED','FAILED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    UNIQUE(group_id,label,random_seed),
    UNIQUE(group_id,random_seed,block_order)
);

ALTER TABLE charm_control.coordination_decisions
    ADD COLUMN IF NOT EXISTS experiment_arm_id uuid REFERENCES charm_control.experiment_arms(arm_id),
    ADD COLUMN IF NOT EXISTS cost_variant text NOT NULL DEFAULT 'no_cost',
    ADD COLUMN IF NOT EXISTS predicted_operational_cost jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS experiment_arms_group_seed_idx
    ON charm_control.experiment_arms(group_id,random_seed,block_order);
