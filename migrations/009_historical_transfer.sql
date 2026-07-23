CREATE TABLE IF NOT EXISTS charm_control.transfer_history (
    history_id uuid PRIMARY KEY,
    source_campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    source_observation_id uuid NOT NULL,
    source_kind text NOT NULL,
    postgres_major integer NOT NULL,
    schema_signature text NOT NULL,
    resource_signature text NOT NULL,
    knob_space_version text NOT NULL,
    objective_definition jsonb NOT NULL,
    constraint_definition jsonb NOT NULL,
    fingerprint_id uuid REFERENCES charm_control.workload_fingerprints(fingerprint_id),
    fingerprint_transform_version text,
    fingerprint_vector jsonb,
    fidelity integer NOT NULL CHECK (fidelity BETWEEN 0 AND 4),
    configuration jsonb NOT NULL,
    throughput_tps double precision,
    p99_ms double precision,
    feasible boolean NOT NULL,
    provenance jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(source_kind, source_observation_id)
);

CREATE TABLE IF NOT EXISTS charm_control.transfer_decisions (
    decision_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    method text NOT NULL,
    equal_budget integer NOT NULL CHECK (equal_budget > 0),
    compatibility_target jsonb NOT NULL,
    accepted_history_ids jsonb NOT NULL,
    compatibility_rejections jsonb NOT NULL,
    similarity_threshold double precision NOT NULL CHECK (similarity_threshold >= 0),
    selected_configurations jsonb NOT NULL,
    decision_details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(campaign_id, method)
);

CREATE TABLE IF NOT EXISTS charm_control.transfer_reports (
    report_id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES charm_control.campaigns(campaign_id),
    equal_budget integer NOT NULL CHECK (equal_budget > 0),
    baseline_method text NOT NULL,
    method_outcomes jsonb NOT NULL,
    negative_transfer jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS transfer_history_compatibility_idx
    ON charm_control.transfer_history(
        postgres_major, schema_signature, resource_signature, knob_space_version, fidelity
    );
