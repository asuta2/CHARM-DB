# Architecture

Status: Slices 1–15 foundations and durable health/default/reload- and restart-tuned worker slices are implemented. The
separate control/target databases, migrations, benchmark evidence, controller/rollback, bounded
knob/index optimization, drift, F0–F4 evidence, prequential calibration/risk persistence,
transfer/coordination/cost decision foundations, worker leases, stale recovery, and campaign
controls, durable managed-index lifecycle reconciliation, a draining continuous worker, exact
orphan-session cleanup, hard resource gates, bounded metrics, and evidence reporting exist.
Multi-objective persistence and one development qLogNEHVI run exist; equal-budget multi-seed
validation, robust cross-context champion selection, dashboards, and long-soak recovery remain
incomplete.

```text
runner/telemetry -> fingerprint -> drift -> history
                                      |
control DB <- API/worker <- optimizer/risk/fidelity
                     |                |
                     +-> PostgreSQL controller -> target PostgreSQL
                     +-> index lifecycle ------> target PostgreSQL
                     +-> reports/raw artifacts
```

The target and control databases use separate persistent stores and credentials. The control database is the authority for campaigns, leases, transitions, observations, actions, calibration, drift, champions, rollbacks, and artifact hashes. The implemented worker claims with `FOR UPDATE SKIP LOCKED`, expiring lease tokens, heartbeats, optional campaign scope, and bounded attempts; persists a transition before each supported action; and uses unique action idempotency records. The continuous service drains a claimed trial after SIGINT/SIGTERM and leaves queued work for a replacement. Long pgbench subprocesses renew the lease at a bounded polling interval, use deterministic per-trial `application_name` values, and remove exact matching orphan target sessions before retry. Pgbench writes an atomic completion marker before the controller records the action result. Knob applications use deterministic application/snapshot IDs and reconcile active PostgreSQL state after an ambiguous commit. Restart-class recovery distinguishes pre-activation from post-activation death, verifies `pending_restart`, and performs one final verified rollback. Managed-index recovery verifies the exact catalog definition and reconciles an already-completed build or drop without repeating DDL; incomplete cost evidence is not invented. Benchmark and index paths capture and enforce pre/post disk, database-size, Docker health/OOM, limit, and memory snapshots. The API exposes bounded-cardinality Prometheus text.

The target is allowlisted to its Docker-network hostname. The API binds to localhost by default. Docker-socket access, if used for target restart and container metrics, grants host-equivalent control and must be isolated through a narrow proxy or documented explicitly.

Trial flow follows the state list in the research specification. Terminal failures remain observations. Last-known-good knobs and retained indexes are captured before mutation; reload/restart readiness and active values are verified; rollback is a persisted stateful action.
