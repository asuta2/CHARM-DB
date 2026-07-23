CREATE UNIQUE INDEX IF NOT EXISTS rollbacks_application_once_idx
    ON charm_control.rollbacks (application_id);
