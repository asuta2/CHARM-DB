CREATE TABLE IF NOT EXISTS charm_control.workload_contexts (
    context_id uuid PRIMARY KEY,
    label text NOT NULL,
    phase_order integer NOT NULL,
    workload_definition jsonb NOT NULL,
    random_seed bigint NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    UNIQUE (label, phase_order, random_seed)
);

CREATE TABLE IF NOT EXISTS charm_control.workload_fingerprints (
    fingerprint_id uuid PRIMARY KEY,
    context_id uuid NOT NULL REFERENCES charm_control.workload_contexts(context_id),
    window_index integer NOT NULL,
    transform_version text NOT NULL,
    feature_names jsonb NOT NULL,
    raw_features jsonb NOT NULL,
    normalized_vector jsonb NOT NULL,
    template_distribution jsonb NOT NULL,
    distance_from_reference double precision,
    captured_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(context_id, window_index)
);

CREATE TABLE IF NOT EXISTS charm_control.drift_events (
    drift_event_id uuid PRIMARY KEY,
    fingerprint_id uuid NOT NULL REFERENCES charm_control.workload_fingerprints(fingerprint_id),
    detector_type text NOT NULL,
    score double precision NOT NULL,
    threshold double precision,
    drift_kind text NOT NULL,
    persistence_windows integer NOT NULL,
    recurring_context_id uuid REFERENCES charm_control.workload_contexts(context_id),
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    detected_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (drift_kind IN ('TRANSIENT','SUDDEN','GRADUAL','RECURRING','STATISTICAL_CHANGE'))
);

CREATE TABLE IF NOT EXISTS charm_control.drift_detector_states (
    state_id uuid PRIMARY KEY,
    detector_type text NOT NULL,
    sequence_number integer NOT NULL,
    state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS fingerprints_context_window_idx
    ON charm_control.workload_fingerprints(context_id, window_index);
CREATE INDEX IF NOT EXISTS drift_events_time_idx
    ON charm_control.drift_events(detected_at);
