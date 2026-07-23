CREATE UNIQUE INDEX IF NOT EXISTS metric_snapshots_trial_phase_source_idx
    ON charm_control.metric_snapshots (trial_id, phase, source);
