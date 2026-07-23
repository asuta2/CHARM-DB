# Operator guide

## Slice 1

Copy `.env.example` to `.env` and set the supplied control DSN plus a target password. The target host must remain in `CHARMDB_TARGET_ALLOWLIST`; the default is localhost/127.0.0.1 only.

Run:

```text
make bootstrap
make up
make seed
make smoke-test
make unit-test
make integration-test
make benchmark-default
```

`make up` starts only the isolated target and applies control migrations. `make down` stops the target without deleting its named volume. Do not use `docker compose down -v` unless intentional data destruction is explicitly approved.

Raw benchmark artifacts live under `artifacts/raw`. The control database stores their relative path, byte size, and SHA-256. A benchmark result is valid only when the JSON artifact, two PostgreSQL snapshots, state transitions, objective values, and matching stored hash exist.

## Safety

The Docker target binds only to `127.0.0.1:55432`, has health checks and resource limits, and is separate from the control database. Never point the target DSN at an unrelated or public database. Keep `.env` private.

Slice 2 supports bounded single-knob mutation and rollback:

```text
uv run charmdb knobs-discover
uv run charmdb knobs-apply random_page_cost 3.9
uv run charmdb knobs-rollback <application-uuid> --reason "operator rollback"
```

Restart-required changes invoke `docker compose restart target-postgres`, wait for readiness, and verify the active setting. The pre-change values are stored as a last-known-good snapshot. Failed post-application verification triggers automatic rollback. Campaign emergency stop and leases cover health, default-benchmark, reload- or restart-class tuned-benchmark, and managed-index lifecycle workflows. Other later-slice CLI commands deliberately return an error.

## Durable health and benchmark workflows

Apply migrations, create a monitor campaign, resume it, enqueue an idempotent health trial, and run
one worker claim:

```text
uv run charmdb migrate
uv run charmdb campaign-create worker-demo --mode MONITOR
uv run charmdb campaign-resume <campaign-id> --reason "start monitor workflow"
uv run charmdb trial-health-create <campaign-id> health-001
uv run charmdb worker-run-once --owner local-worker-1
uv run charmdb trial-history <trial-id>
```

Use `campaign-pause`, `campaign-resume`, `campaign-stop`, and
`campaign-emergency-stop`. Emergency stop persists cancellation transitions and clears leases for
all incomplete trials in that campaign.

For continuous campaign-scoped processing, run:

```text
uv run charmdb worker-run --owner local-service-1 --campaign-id <campaign-id> \
  --lease-seconds 120 --poll-seconds 1
```

SIGINT or SIGTERM requests a drain: an already claimed trial finishes, the service emits its final
bounded JSON event and summary, and queued trials remain claimable by a replacement. Use
`--max-trials` or `--idle-exit-seconds` for bounded automation. Do not expect a signal to interrupt
an active pgbench subprocess.

Enqueue a controlled default or tuned benchmark after resuming a research campaign:

```text
uv run charmdb trial-baseline-create <campaign-id> baseline-001 --duration-seconds 5
uv run charmdb trial-tuned-create <campaign-id> random_page_cost 3.9 tuned-001 --duration-seconds 5
uv run charmdb trial-tuned-create <campaign-id> shared_buffers 20480 restart-001 --duration-seconds 5
uv run charmdb worker-run-once --owner local-worker-1 --lease-seconds 120
uv run charmdb trial-history <trial-id>
```

The worker persists each transition before the next operation. Completed pgbench execution is
written to an atomic marker before its database action is finalized, allowing an ambiguous result
to be recovered without rerunning the workload. A tuned trial uses deterministic controller IDs,
reconciles requested/previous/active values after lease loss, verifies activation, and always
restores the saved setting after measurement. The target must begin at the recorded boot defaults.
Postmaster settings restart only the isolated Docker target, wait for readiness, verify the active
value and cleared `pending_restart`, and restart again during rollback. Do not submit restart-class
work unless Docker access and the rollback path are available.

Enqueue a bounded managed-index lifecycle only after the table exists:

```text
uv run charmdb trial-index-create <campaign-id> charm_index_fixture customer_id index-001 \
  --include-columns id,amount
uv run charmdb worker-run-once --owner local-worker-1
```

The worker verifies the exact table, B-tree keys, `INCLUDE` columns, validity, and readiness. It
then removes only the registered `charm_idx_` index and persists build time, WAL, size, drop time,
and a hash-referenced JSON artifact. This lifecycle validates mutation durability; it does not
measure query benefit.

Before and after durable benchmark/index work, the worker validates artifact and target disk
headroom, target database size, Docker running/health/OOM state, memory use and configured limits.
Thresholds are `CHARMDB_MIN_HOST_FREE_BYTES`, `CHARMDB_MIN_TARGET_FREE_BYTES`, and
`CHARMDB_MAX_CONTAINER_MEMORY_FRACTION`. A violation is persisted as
`RESOURCE_LIMIT_EXCEEDED`; inspection failure is fail-closed. A Docker health state of `starting`
immediately after a controlled restart receives only a bounded settle interval.

Pgbench phases use exact names of the form `charmdb:<trial-id>:warmup` and
`charmdb:<trial-id>:measurement`. Before retry, the worker terminates only target sessions matching
that exact phase name. This does not guarantee cleanup of an orphaned client OS process.

The API exposes Prometheus text at `/metrics`. Labels are restricted to bounded campaign status,
workflow, and trial-state dimensions; no dashboard or alert deployment is currently included.

## Constrained single-objective development campaign

```text
uv run charmdb tune-single --sobol-trials 4 --bo-trials 1 --seed 20260712
```

The command tunes only `random_page_cost`, `work_mem`, and `effective_io_concurrency`. It applies and verifies each candidate, runs the development benchmark, persists objectives, constraints, acquisition metadata, and training-observation IDs, then restores defaults. The development constraint is p99 at most 20 ms with zero failed transactions. Do not treat a one-seed development campaign as a performance conclusion.

## Constrained multi-objective development campaign

```text
uv run charmdb tune-multi --sobol-trials 4 --bo-trials 1 --f4-replicates 2 \
  --seed 20260730 --context-label development-pgbench-scale10
uv run charmdb pareto-front <campaign-id>
uv run charmdb champion <campaign-id>
uv run charmdb recommendation-verify <recommendation-id>
uv run charmdb gate-status
```

The primary acquisition is qLogNEHVI; pass `--acquisition qLogNParEGO` for the sensitivity family.
The command persists the fixed reference point, recommendation identity, exact training IDs,
context/fidelity, Pareto snapshots, and repeated-F4 selection evidence. A selection with only one
observed workload context is reported as `CANDIDATE`, never a validated robust champion. The current
implementation uses the synchronous controller/benchmark path, so run it only on the isolated
target with Docker resource inspection and rollback available.

`gate-status` is read-only. When either the calibration activation gate or paired-fidelity gate is
closed, future recommendations persist `FULL_F3_REQUIRED`; prediction cannot bypass the empirical
prerequisites.

Preregister and inspect the full comparison matrix without executing it:

```text
uv run charmdb experiments-register
uv run charmdb experiments-status
uv run charmdb experiment-gates
uv run charmdb analysis-readiness
uv run charmdb analysis-generate
```

Registration is idempotent and creates 19 groups/168 `PLANNED` arms with three fixed seeds. It does
not launch campaigns. Analysis generation writes and hash-registers an `INCOMPLETE` readiness file
while any arm lacks a completed linked campaign; it must not emit comparative effectiveness claims.

Before a real search arm can receive a campaign, capture and validate the logical snapshot, run the
exact-profile five-repetition reference block, then derive and independently review the execution
file from persisted evidence:

```text
uv run charmdb experiment-manifest-preflight
uv run charmdb experiment-dataset-restore-validate <preflight-id>
uv run charmdb experiment-f3-reference-run <preflight-id> --repetitions 5
uv run charmdb experiment-manifest-draft <preflight-id> <arm-id>
uv run charmdb experiment-manifest-review <preflight-id> <arm-id> --manifest <execution.json>
uv run charmdb experiment-arm-prepare <arm-id> --manifest <execution.json>
uv run charmdb experiment-arm-start <arm-id>
uv run charmdb experiment-search-arm-run <arm-id>
uv run charmdb experiment-arm-status <arm-id>
uv run charmdb experiment-arm-budget-record <arm-id> <terminal-trial-id> --phase static
uv run charmdb experiment-arm-finalize <arm-id>
```

Drafting supports the next registered `search_methods` arm only. Review re-derives the file,
checks its hashes against the preflight/reference/restore evidence, verifies current target and
resource safety, and persists the result. Preparation rejects an evidence-derived file without a
matching passing review and freezes it only after its gate passes; a trigger rejects later
rewrites. A closed prerequisite gate records `BLOCKED` but freezes no manifest and creates no
campaign. The first start restores and content-validates the frozen snapshot once for the arm
before position 0. Finalization requires a linked campaign, only terminal trials, and realized
budget from 20 through 21 F3-equivalent units. The manual budget operation is recovery/operator
tooling; method orchestrators normally charge terminal trials automatically.

For a prepared `search_methods` arm, `experiment-arm-start` is also the resume command. It enforces
the registered within-seed block order, returns an existing active trial idempotently, reconciles a
terminal trial before scheduling its successor, and finalizes only after the frozen realized budget
passes. Failed/early work may therefore require more than 20 positions. Run the durable worker against the returned campaign/trial;
worker terminal paths attempt automatic ledger reconciliation. Do not use this command for advanced
groups whose prerequisite gates or orchestrators remain incomplete.

`experiment-search-arm-run` drives that same idempotent start/worker/reconcile loop until the arm
finalizes. Interrupting it does not erase progress; rerun the command against the same arm. The
one-step `experiment-arm-start` remains useful for inspection and external worker operation.

## Managed index lifecycle experiment

```text
make index-seed
make index-experiment
```

This creates or extends only `public.charm_index_fixture`, generates a deterministic covering B-tree candidate, measures a query and rolled-back write batches, builds the index, verifies planner use, repeats measurements, drops the managed index, and measures the query again. The index must be absent afterward. CHARM-DB refuses to drop indexes without both its managed prefix and control record.

## Controlled workload drift

```text
make evaluate-drift
```

The default experiment runs 12 read-heavy, 12 write-heavy, and 8 recurring-read windows. For stronger ADWIN evidence, use `uv run charmdb drift-experiment --baseline-windows 32 --drift-windows 32 --recurring-windows 16`. Each window resets only `pg_stat_statements`; configuration and indexes remain unchanged. Contexts, fingerprints, distances, detector state, and drift events are stored in the control database.

## Multi-fidelity pilot

```text
make fidelity-pilot
```

## Calibration and risk pilot

Run only after migrations, a default F3 baseline, and paired-fidelity evidence exist:

```text
make calibration-pilot
```

The command defaults to 16 new pairs so four historical pairs plus four new training-only pairs
precede 12 scored candidates. Forecasts are committed before F3. A completed run does not imply
that probabilistic promotion is enabled; inspect the stored activation gate and class counts.

The default executes four F0/F1/F2/F3 pairs and two F4 repeats of the best F3 candidate. It takes roughly four minutes on the inspected host. Every candidate is restored before the next. F1 is planner cost only. The command does not enable live early stopping unless the persisted evidence gate has at least 12 pairs, adequate rank correlation, and no false negatives.

## Reports and timed soak execution

Generate a reproducible evidence inventory:

```text
make report
```

The command writes Markdown, JSON, CSV, an SVG chart, and `manifest.json` below
`artifacts/reports`. JSON includes multi-objective run/recommendation/Pareto/champion lineage; the
manifest stores exact queries, filters, ordering, and source primary keys. The global chart is
descriptive and may mix incomparable campaigns; use filtered equal-budget analysis for thesis
claims.

Run an explicitly timed campaign-scoped health soak:

```text
make soak-test DURATION=1h
make soak-test DURATION=24h
```

Durations accept positive seconds (`30s`), minutes (`15m`), or hours (`1h`). Do not report a soak
duration unless the command completes and its returned `elapsed_seconds` meets the request. Only a
two-second command validation has been executed in the current evidence ledger.
