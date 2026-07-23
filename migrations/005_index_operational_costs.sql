ALTER TABLE charm_control.index_measurements
    ADD COLUMN IF NOT EXISTS before_write_median_ms double precision,
    ADD COLUMN IF NOT EXISTS after_write_median_ms double precision,
    ADD COLUMN IF NOT EXISTS drop_duration_seconds double precision;
