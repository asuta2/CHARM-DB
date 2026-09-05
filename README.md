# CHARM-DB

CHARM-DB means **Contextual, Holistic, Adaptive, Risk-aware, Multi-fidelity Database Tuning**. It is a planned research platform for safe, sample-efficient PostgreSQL knob-and-index optimization.

## Current status

Protocol-v2 checkpoint D064 (2026-09-05) has implemented and live-validated the
separately pre-registered Wave B continuation. Migration 041, the 262-slot deterministic
design, durable runner, standalone two-seed report, and subsequent combined five-seed
report are ready under manifest SHA-256 `35d0b57b...3c3e18`. D065 created the only
Wave B campaign, `8d95edea-81e7-41eb-ba81-0997008cf4a8`, and audited all 262 frozen
slots with zero observations. The approximately 121.010-hour run is now at the external
operator boundary. Apply-best remains deferred.

The mandatory checkpoint was confirmed on 2026-07-12. Slices 1–15 foundations are implemented: isolated target/control databases, bounded knob/index optimization, fingerprints and drift detection, an explicit F0–F4 ladder, prequential calibration/risk comparison, compatibility-gated transfer, coordinated schedule construction, and measured-cost accounting. The durable worker now executes read-only health checks, default benchmarks, bounded reload- or restart-class knob benchmark trials, and managed index build/drop trials with renewable leases, stale recovery, idempotent state actions, atomic workload-completion markers, verified mutation, and final rollback/cleanup. A continuous service drains safely on signals, emits bounded JSON events, removes exact per-trial orphan database sessions, and applies pre/post disk/OOM/health/memory gates; bounded Prometheus application metrics are exposed. A migration-backed multi-objective development path now persists qLogNEHVI/qLogNParEGO recommendation identity, feasible Pareto snapshots, explicit context/fidelity, reconstruction metadata, and repeated-F4 candidate evidence. Its only executed multi-objective run is one-context development evidence, not an effectiveness result or robust champion. The risk gate remains closed, and Slices 13–15 do not yet have prospective equal-budget performance evidence.

Read these checkpoint documents first:

- `run.md` (new-PC migration and benchmark continuation)
- `docs/implementation-overview.md`
- `docs/scientific-positioning.md`
- `docs/objective-strategy.md`
- `docs/experimental-methodology.md`
- `DECISIONS.md`
- `KNOWN_LIMITATIONS.md`

## Verified environment (2026-07-12)

- Windows host, 12 logical processors and 17,085,444,096 bytes physical RAM
- Docker Desktop engine 28.2.2 on WSL2, 12 CPUs and 8,282,882,048 bytes RAM
- C: drive: 930.65 GiB total and 104.58 GiB free at inspection
- Python 3.12.6, uv 0.11.28, PostgreSQL client 18.4, Docker Compose 2.37.1
- supplied database: PostgreSQL 18.4 at `localhost:5432/charmdb`, empty `public` schema, about 8 MiB
- available but not installed in the supplied database: `pg_stat_statements`, `pg_buffercache`, and `pg_prewarm`; HypoPG is not available there

These are inspection facts, not benchmark conditions. Every experiment must record a fresh resource snapshot.

## Deployment split

The supplied host PostgreSQL database is the **control database**. The system under test is a separate PostgreSQL 18.1 Docker image pinned to digest `sha256:5773fe...b73a2`, with a named volume, 4 CPU/4 GiB limits, health check, and a localhost-only port. This preserves tuner state outside the database being tuned. Credentials are read from `.env`, which is ignored; `.env.example` contains placeholders only.

## Slice 1 quick start

1. Copy `.env.example` to `.env` and set both database passwords/DSNs.
2. Run `make bootstrap`.
3. Run `make up`.
4. Run `make seed`.
5. Run `make smoke-test`, `make unit-test`, and `make integration-test`.
6. Run `make benchmark-default`.

Raw runs are written below `artifacts/raw/<campaign-id>/<trial-id>/` and referenced by SHA-256 from the control database. Generated reports include an exact query/filter/source-key manifest.

## Commands

Implemented: `make bootstrap`, `make up`, `make down`, `make seed`, `make index-seed`, `make index-experiment`, `make fidelity-pilot`, `make calibration-pilot`, test/smoke commands, `make benchmark-default`, `make tune-single`, `make evaluate-drift`, `make report`, and the timed `make soak-test` runner, plus knob-controller, campaign-control, durable baseline/tuned trial creation, trial/drift/index/Pareto/champion/coordination history, one-shot and continuous campaign-scoped workers, `tune-multi`, recommendation verification, conservative optimizer and experiment-prerequisite gate status, immutable experiment manifests, logical snapshot/restore validation, exact-profile F3 reference blocks, independently persisted manifest reviews, per-arm dataset restores, realized-budget ledgers, fail-closed arm completion, deterministic durable search-method arm start/resume and whole-arm execution with automatic terminal-trial budget reconciliation, the preregistered experiment matrix, fail-closed analysis readiness, API manual rollback, and `/metrics`. Real random-search and constrained-BO arms completed at their frozen budgets; no equal-budget comparative effectiveness result exists yet. Coordinated live tuning, full evaluation, and ablation execution still fail explicitly rather than pretending to run.

## Evidence policy

Only saved observations generated by an actual run may enter results. Planner cost is not runtime performance; reduced fidelity is not full fidelity; failed and unsafe trials remain in analysis; and CHARM-DB uses “Contextual,” not “Causal.”

The migration-024 Docker integration gate passed 25/25 at E032 with a clean post-suite target
audit. This validates the current-host integration path, not tuning effectiveness or clean-machine
reproduction.

E040 audited the recovered constrained-BO arm `c19d3a94-…`: all 19 positions, artifacts, acquisition
lineage, retry accounting, rollbacks, budget entries, and final target invariants passed. It reached
20.25832481828215 units with 1 feasible and 18 infeasible outcomes, bringing readiness to 2/168.
E041 permanently fixed the own-prepared-campaign restore guard, added live evidence-lineage
coverage, and passed all 92 unit and 26 integration tests plus formatting, Ruff, strict mypy, and a
clean post-suite audit. Canonical preflight `ea3506aa-…` passed restore validation. E042 then
independently reviewed and prepared standard-BO arm `2fe5cbf2-…`; its external command is in
`handoff.md` and no position has started.

E037 adds `run.md`. A new PC is treated as a new benchmark context: preserve old history, but use a
fresh control database and artifact root, recapture reference evidence, and restart the search
block. Directly continuing old-host arm state after a host move is exploratory, not a valid
cross-method comparison.

Long experiment arms remain operator-run. While an arm is active, do not migrate, export databases,
run tests, stop Docker, or start another orchestrator; report completion so its durable result can
be audited.

Protocol-v2 D031 is the current local continuation milestone. It preserves the
failed D030 parameter-screening ledger, adds a PostgreSQL PID-1 mitigation via
Compose `init: true`, and gates exact replacement observations 34–35 behind
three consecutive infrastructure-only restore/fingerprint validations. See
`v2/docs/operator-runbook.md` and `v2/config/parameter-screening-recovery.json`.
D031 failed closed on inherited orphan relation files; D032 now registers a
snapshot-backed rebuild of only the synthetic target database and a fresh
three-pass validation. No recovery benchmark has been created.
D032 subsequently passed remediation and all three restores; the two exact tail
observations completed. D033 remains `BLOCKED_DRIFT` because p99 fitted control
change was 6.438 ms against the frozen 5 ms gate. D035 records an explicit
post-result, time-constrained amendment: D034 is retired unexecuted; the 32
Sobol plus three-control block is final for screening; and an exploratory,
drift-limited eight-knob space is frozen. Primary Wave A is 30 candidates per
method over three seeds, with two seeds reserved for a possible later Wave B.
D036 freezes the honest Bayesian-method reframe as throughput qLogNEI versus
multi-objective qLogNParEGO/qLogNEHVI under common hard gates. Primary execution
was then blocked on the durable interleaved runner. D037 hash-freezes the
deterministic 393-observation Wave A schedule. D038 implements migration 037,
the eight-dimensional runner, isolated BO lineage, retained infrastructure
attempts, and drift-adjusted analysis; the fixed candidate design is separately
frozen at `6664b06c…19c143e`. D039 applied migrations 037–038, passed the live
schema lifecycle and complete integration suite, added a resumable whole-Wave-A
driver, copied runtime evidence to `C:\CHARMDB-ARTIFACTS\v2`, and verified more
than 50 GiB free with a clean target. D040 closed the restore-reliability gate:
the first prepared soak is retained as terminal `FAILED` evidence and a
superseding soak passed 15/15 consecutive restore/fingerprint cycles. D040 also
made the frozen evidence specification executable, adding 30-slot logical
trajectories, completed method and safety tables, nondominated-front extraction,
six deterministic SVG figures, a hashed UTF-8 report tree, and a synthetic
393-slot rehearsal that proves the whole path before any real observation. The
manifest is `ready` with `execution_ready=true` after D041 recorded P007. D042
hardened durable-action lease renewal after session 01 lost one observation to a
heartbeat failure; no observation was altered. D044 audits the completed session
02: positions 15--147 all completed, including the attempt-2 recovery of
position 15 and all 54 first-seed adaptive BO slots. The single campaign
`b0619879-c807-4ee3-859e-2c2dbec9934e` remains `RUNNING` at 147/393 completed
observations. Those completed slots have passed candidate restores, verified raw
artifacts, frozen-plan conformance, valid metrics, and valid adaptive training
lineage; the target is clean. D045 then audited session 03: following an
environment-level DLL-policy startup incident that created no observation, the
rerun completed positions 148--175. The campaign is `RUNNING` at 175/393
completed observations (seed 1 complete; seed 2 at 44/131), with 176 append-only
attempts, 58 completed adaptive proposals, and 1,156 valid lineage rows. The
ledger remains partial, so no method result is yet supported. D046 then audited
session 04: positions 176--208 completed on attempt 1 in 33 clean durable steps.
The campaign is `RUNNING` at 208/393 (seed 2 at 77/131), with 209 append-only
attempts, 77 completed adaptive proposals, 1,460 valid lineage rows, and 208
artifact records independently verified by path, size, and SHA-256.

D047 then audited session 05: positions 209--242 completed on attempt 1 in 34
clean durable steps. The campaign is `RUNNING` at 242/393 (seed 2 at 111/131),
with 243 append-only attempts, 96 completed adaptive proposals, 1,884 valid
lineage rows, and 242 artifact records independently verified by path, size,
and SHA-256.

D048 audited terminal Wave A execution. Session 06 completed positions 243--260
before its deadline, and the operator continuation completed positions 261--393
and emitted `observations-complete` at 2026-08-31 09:40 CEST. The primary block
is `OBSERVATIONS_COMPLETE`; the campaign is `PAUSED`; all three seeds and all
393 immutable slots are complete. The 394-attempt ledger retains 393 completed
attempts and the single D042 infrastructure failure. All 393 restores, metrics,
artifact hashes, frozen schedule fields, fixed candidates, and 3,321 lineage
rows passed the terminal audit. Wave A execution is complete but its frozen
analysis and report have not run, so no comparative method or thesis outcome is
yet authorized.

D049 executed the frozen fail-closed terminal sequence on 2026-08-31. The block
is `ANALYZED`, the campaign is `STOPPED`, and the outcome is
`COMPLETE_WITH_DRIFT_FLAGS`: seed 88408573 raised the transparency-only 5% TPS
drift flag; the other two seeds are unflagged; nothing was discarded. Under the
frozen drift-adjusted contrasts, all three BO methods hold throughput parity
with the contemporaneous default (mean -0.26% to -0.49%) while improving mean
p99 by 1.02-1.20 ms and dominating the local default in 19-21 of 30 candidates
per seed; random/Sobol average 7.28-7.38% below the local default's throughput
and dominate in only 2-6 of 30. The multi-objective methods lead hypervolume
and minimum p99 in every seed. The improvement-over-default claim is
latency-side/Pareto expansion at throughput parity, not TPS dominance.

D050 adjudicated the seed-1 showcase proposals against the full three-seed
results and registered the secondary display set S1-S6/S8 with frozen formulas
in `v2/docs/evidence-and-figures.md`; the wall-clock efficiency curve was rejected
because per-slot cost is method-flat. All showcases are exploratory; the five
frozen endpoints and the deterministic report remain the primary evidence.

D051 implements those frozen displays as an authenticated offline renderer and
executes it against the unchanged D049 export. The deterministic 19-file tree is
under `primary-comparison/report-secondary/` (index SHA-256
`b3f0f6c0f661be12871d8744db2ba90ee93f386903070e0a879c4c2ee3a7aabc`); it contains
nine CSV tables, eight SVG displays, and an explicitly exploratory Markdown report.
The next v2 work is Multi-fidelity Phase A pre-registration (P009).

Agents may run test suites. Agents must never run experiment-arm benchmarks: they must stop at a
durable checkpoint and provide the user the exact arm command, prerequisites, expected duration,
log/artifact paths, and terminal markers, then read the saved results after the user completes it.
Non-test benchmarks, reference blocks, soaks, and other programs expected over 30 minutes use the
same external handoff unless the user explicitly changes that rule. Agents do not poll external
runs.
