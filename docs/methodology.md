# Methodology

Protocol v2 compares random search, Sobol search, throughput-only qLogNEI,
scalarized multi-objective qLogNParEGO, and Pareto/hypervolume qLogNEHVI at an
equal candidate budget. PostgreSQL default is sampled separately as a blocked
reference and drift control. It neither consumes candidate slots nor trains an
optimizer.

p99 latency is an objective, not an SLO constraint. Feasibility means a valid
measurement interval, zero benchmark failures, and passed universal hard safety gates.
Candidate learned constraints require calibration support; none was found. The decision
was therefore to use no learned constraint and remove constrained method labels.
Universal hard safety gates remain identical across all methods.

## Evidence roles

Every v2 run is assigned exactly one persisted role:

- `INFRASTRUCTURE`: pipeline and restore correctness only.
- `CALIBRATION`: workload, warm-up, duration, and screening decisions.
- `HISTORICAL_DIAGNOSTIC`: old-machine evidence, never pooled with v2 results.
- `PRIMARY`: frozen five-method comparison only.
- `SECONDARY`: retrospective or approved live multi-fidelity evidence.
- `F4_CONFIRMATION`: repeated validation of finalists.

Primary evidence is permitted only after the benchmark profile and restore
policy are frozen, the final default reference block passes, screening is
complete, and the full primary protocol is frozen.

## Reproducibility rules

Migrations 001–025 are historical and hash-checked; v2 database changes are
forward-only from 026. Each evidence-producing observation starts from a
verified candidate-level dataset restore. Raw transaction logs, timing phases,
configuration vectors and named values, fingerprints, safety outcomes, method
accounting, execution order, and environment provenance are retained.

Previous-machine performance data may explain earlier weaknesses but cannot
support final v2 comparative claims.

## Parameter screening

The screening decision fixes a fresh screening seed and 32 joint scrambled-Sobol
configurations over the 11-knob bounded pool. PostgreSQL-default controls occur at
positions 1, 18, and 35. Screening evidence is `CALIBRATION` only and cannot train or
enter the primary comparison.

The primary sensitivity statistic is the maximum absolute partial rank
correlation coefficient for TPS and p99 after controlling for the other ten
ranked knobs. The inclusion threshold is 0.20. Scores from 0.15 through 0.25
are ambiguity candidates; no more than the three closest to 0.20 may receive a
paired low/high endpoint check, keeping follow-up to six observations. The
final space must contain 8–12 operationally safe knobs and always includes
`shared_buffers`. Durability-reducing settings remain excluded.

At least 28 of the 32 Sobol configurations must yield valid measurements.
Failed or invalid configurations are retained and never replaced. Fewer valid
measurements require a bounds review under a new pre-registration. Control
drift above 5% TPS or 5 ms p99 blocks interpretation rather than triggering
extra observations.

Migration 032 persists the full initial schedule before work begins and guards
all block/run transitions. Each slot uses a verified candidate-level logical
restore, unconditional restart, 600-second warm-up, and 600-second measurement.
Infrastructure failures in restore or baseline verification stop for recovery
without being reclassified as parameter evidence; candidate-caused terminal
outcomes remain in their original slots. Analysis can add only the OAT pairs
selected by the frozen screening rule.

The initial screening campaign stopped after immutable positions 1–33 when position 34
exhausted its restore retries. The recovery decision preserves those rows. A
supplemental ledger first requires three consecutive infrastructure-only
restore/fingerprint passes with the exact PostgreSQL 18.1 image and Compose init
mitigation. Only then may it measure exact original positions 34 and 35. The combined
analysis uses the original positions 1–33 plus linked recovery positions 34–35, records
the chronology gap, and applies every registered validity, drift, PRCC, dimension, and
OAT rule unchanged. The recovery init mitigation removed the restore crash, but its
first validation found unreferenced relation files from the original campaign through
the whole-database physical fingerprint. The remediation decision permits only a
snapshot-backed rebuild of the synthetic target database, then repeats the full
three-pass gate; this is infrastructure evidence and does not alter the calibration
schedule or interpretation rules. The screening analysis applied that rule: all Sobol
points were valid and TPS control drift passed, but p99 fitted control change was 6.438
ms. This exceeds 5 ms, so the screening remains a terminal `BLOCKED_DRIFT` result rather
than a passed pre-registered screening.

An operator-directed decision under thesis time constraints permits an exploratory
post-result amendment. It retires the unexecuted temporal-stability block, treats the
existing 32 Sobol measurements and three controls as final for this phase, and uses them
only as exploratory, drift-limited evidence for the next prospective stage. No OAT was
run. Applying the frozen 0.20 PRCC threshold, mandatory `shared_buffers`, and
eight-dimension minimum selects `shared_buffers`, `effective_cache_size`, `work_mem`,
`checkpoint_timeout`, `checkpoint_completion_target`, `max_wal_size`,
`random_page_cost`, and `max_parallel_workers_per_gather`. The last is above threshold;
`checkpoint_timeout` is the highest-scoring remaining safe knob added to reach eight and
must be described as weak, duration-dependent evidence.

The primary design now uses Wave A of 30 slots per method over three seeds.
Within each seed, the three Bayesian methods share 12 initial observations and
receive 18 method-specific adaptive slots; random and Sobol each receive 30.
Five default controls remain interleaved. The two unused reserved seeds are reserved
for a separately authorized Wave B and cannot be chosen based on Wave A's
direction; Wave A must be reported on its own before any five-seed pooling.

A separate decision authorizes preparation and later external execution of Wave B under
the separate [Wave B
pre-registration](../experiments/thesis/preregistration/wave-b-final-analysis-preregistration.md).
Wave B uses exactly the untouched seeds `1902413987` and `740267717`, 131 physical
observations per seed, and no change to the Wave A methods, budgets, benchmark profile,
search space, endpoint formulas, reference point, validity, retry, drift, or
multiplicity rules. Its deterministic schedule and fixed-candidate hashes must be frozen
before campaign creation. Wave B is reported separately before the combined analysis;
five-seed pooling combines five seed-level values, not candidate rows. The seed remains
the independent unit.

The final contribution is framed as reliable budget allocation. The primary
question is whether BO repeatedly concentrates candidates near the local
default and avoids the roughly 7% mean TPS degradation observed for random and
Sobol in Wave A, not whether one method owns the single highest raw-TPS point.
The two multi-objective acquisitions are reported individually but interpreted
as a family because their leave-one-seed-out ordering is unstable. P99 is
reported in milliseconds and differences around 1 ms at roughly 26 ms are
described as consistent but modest.

The completed calibration had zero benchmark failures in all 35 observations
and did not straddle a defensible candidate memory-headroom boundary. The decision
therefore applies the pre-specified honest fallback: compare throughput-only
qLogNEI with two multi-objective Bayesian acquisitions, qLogNParEGO and
qLogNEHVI, under common hard gates. This keeps five equal-budget methods and
turns the Bayesian comparison into a direct single-objective versus
multi-objective question without claiming learned constraint evidence.

The primary protocol implements the result-blind execution and analysis mechanics.
Random, Sobol, and the 12 shared BO points are generated in the frozen eight-dimensional
space and hash-bound before execution. Each adaptive BO fit uses only completed, valid
observations from the same seed and method, plus that seed's shared initial points.
Default controls, rival-method observations, candidate failures, and infrastructure
failures never enter training.

The proposed primary drift interpretation uses piecewise-linear interpolation
of the five within-seed default controls for candidate-relative TPS and p99.
Least-squares fitted endpoint changes over all valid controls raise visible 5%
TPS/5 ms p99 flags but do not terminate or delete the 181-hour campaign. Raw and
adjusted results are both mandatory. Retryable restore/fingerprint failures use
the same persisted candidate for at most three one-attempt trials and consume no
new method slot. These policy details remain subject to supervisor
confirmation; implementation does not authorize primary execution.

The pre-launch reliability decision adds a separate `INFRASTRUCTURE` reliability ledger
before authorization. It requires 15 consecutive logical restore, exact-core
fingerprint, physical- statistics fingerprint, pinned-image, and Docker-init passes
under the same configured non-OneDrive artifact root that Wave A will use. A failure
terminates that soak attempt and remains visible; it cannot be averaged away. The soak
is not optimizer training, candidate budget, calibration, or PRIMARY evidence.