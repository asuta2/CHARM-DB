# Persistence and recovery

The control database stores immutable schedules, candidate decisions, attempts, leases, state actions, configuration snapshots, and analysis lineage. Raw artifact files hold measurements and completion markers with SHA-256 identities. Recovery reconciles persisted actions and interrupted attempts; the optimizer reconstructs eligible observations from saved evidence rather than loading a pickle or Torch checkpoint.

Each measurement stage requires its own readiness gate and candidate-level restore/fingerprint verification. An infrastructure interruption stays in the same immutable slot for exact retry; a terminal candidate outcome consumes its slot. Worker leases and heartbeat protect single ownership. Configuration activation, active-value verification, and rollback are recorded as durable transitions. Historic normalized SQL migration checksums, table names, workflow values, evidence roles, UUID derivations, and marker formats are storage contracts; the migration changes only code organization.

The [D070/D071 apply-best workflow](apply-best.md) separately persists its pre-activation snapshot and exact rollback command. The [reproducibility guide](reproducibility.md) explains artifact/backup limits and source hashing.
