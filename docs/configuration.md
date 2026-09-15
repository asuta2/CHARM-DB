# Configuration

`.env.example` lists the supported `CHARMDB_*` variables. `CHARMDB_CONTROL_DSN` points to the durable control database; `CHARMDB_TARGET_DSN` and `CHARMDB_TARGET_HOST/PORT/DB/USER/PASSWORD` describe the isolated PostgreSQL target. They must be separate. `CHARMDB_TARGET_ALLOWLIST` restricts accepted target hosts. `CHARMDB_ARTIFACT_DIR` is the authenticated raw-evidence destination; use an independent directory with adequate free space, not a source or legacy `artifacts` subtree.

The example benchmark warm-up, duration, concurrency, and seed values are development defaults. Frozen thesis observations use the manifest/profile's scale-500, concurrency-32, four-thread, 600-second warm-up/F3 measurement contract; environment defaults do not override that scientific identity. Host/target free-space and container-memory thresholds are pre/post safety gates. Subprocess credentials are supplied through settings/environment rather than tracked files. Do not place real passwords in `.env.example`.

The [frozen manifest inventory](../experiments/thesis/frozen-sha256.json) records historical logical artifact and repository path labels. Runtime paths are resolved from `CHARMDB_ARTIFACT_DIR` and the repository resolver without editing those immutable files.
