CREATE TABLE IF NOT EXISTS charm_control.analysis_reports (
    analysis_report_id uuid PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('INCOMPLETE','COMPLETE')),
    independent_unit text NOT NULL,
    registered_groups integer NOT NULL CHECK (registered_groups >= 0),
    registered_arms integer NOT NULL CHECK (registered_arms >= 0),
    completed_arms integer NOT NULL CHECK (completed_arms >= 0),
    missing_arms jsonb NOT NULL,
    methods jsonb NOT NULL,
    relative_path text NOT NULL,
    sha256 text NOT NULL,
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
