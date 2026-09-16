# Thesis evidence closeout

As of 2026-09-14, the narrowed protocol-v2 experiment program and the authorized
champion deployment are complete. The evidence is sufficient to write the bounded
five-method OLTP comparison, subject to the safety-accounting correction below.
This is not completion of the original CHARM-DB platform research program or a
claim of independent clean-machine reproduction.

## Contribution and results text

CHARM-DB evaluates how Bayesian optimization allocates a limited PostgreSQL tuning
budget relative to random and Sobol search. It compares throughput qLogNEI,
multi-objective qLogNParEGO, and multi-objective qLogNEHVI under the same execution
validity and hard safety gates. It does not claim a learned constraint model.
Candidate-level logical restoration, unconditional restart, frozen warm-up and
measurement windows, interleaved default controls, and durable attempt history
make the controlled comparison auditable.

The final cohort contains five independent seeds, 30 logical candidate slots per
method per seed, and 655 physical observations: 630 candidates and 25 controls.
The three BO methods share 12 initial observations per seed and each receives 18
adaptive proposals. Consequently 750 logical method slots correspond to 630
physical candidate measurements; shared observations are not extra independent
replicates. The default consumes no optimizer budget. All 655 observations
completed validly, with two retained infrastructure failures and exact retries.

The tested profile is `scale500-c32-w600-f3-600-v1`: PostgreSQL 18.1, pgbench's
TPC-B-like workload at scale 500, concurrency 32, four client threads, a 600-second
warm-up and 600-second F3 measurement, on the recorded 4-CPU/4-GiB container context.
This is a local research workload, not an official audited TPC result. The eight knobs
are `shared_buffers`, `effective_cache_size`, `work_mem`, `checkpoint_timeout`,
`checkpoint_completion_target`, `max_wal_size`, `random_page_cost`, and
`max_parallel_workers_per_gather`. Durability protections are not tuned away. See the
[frozen profile](../../../docs/frozen-benchmark-profile.md) and
[methodology](../../../docs/methodology.md). The decision was to preserve durability
settings and apply the same validity and safety gates to every method.

The strongest result is reliability of proposed candidate quality. BO candidates
dominated their same-seed interpolated local default much more frequently than
random or Sobol candidates. The final five-seed values below come from the
authenticated final analysis, not the earlier three-seed summary.

| Method | Locally dominating candidates | Mean control-relative TPS | Mean control-relative p99 |
|---|---:|---:|---:|
| Random | 20/150 (13.3%) | -8.322% | +1.304 ms |
| Sobol | 28/150 (18.7%) | -7.413% | +1.364 ms |
| Throughput qLogNEI | 99/150 (66.0%) | -0.267% | -1.395 ms |
| qLogNParEGO | 102/150 (68.0%) | -0.272% | -1.530 ms |
| qLogNEHVI | 95/150 (63.3%) | -0.946% | -1.232 ms |

Lower p99 differences are better. The fraction of locally dominating candidates
is descriptive; 150 candidates are not 150 independent campaign replicates.
The findings support more reliable budget allocation and degradation avoidance
in this context. They do not establish universal raw-throughput superiority or
a qLogNParEGO-versus-qLogNEHVI winner. The five registered endpoints retain
seed-level uncertainty and paired comparisons in the final report. With five
paired seeds, the registered exact two-sided test has a minimum unadjusted
p-value of 0.0625; no conventional p<0.05 superiority claim follows. Endpoints
derived from TPS and p99 are not independent confirmations.

The outcome remains `COMPLETE_WITH_DRIFT_FLAGS`. Seed 88408573 exceeded the
transparency-only TPS drift threshold; seed 740267717 exceeded the p99 drift
threshold (9.002 ms fitted change). These observations remain included. The
initial screening block separately failed its original p99 drift gate and was
used only under the explicit exploratory screening amendment; no OAT was run.

Retrospective multi-fidelity Phase A examined 60/120-second prefixes of all 393
physical observations in the initial three-seed cohort. Both windows passed the
registered per-seed correlation and zero-false-rejection gates. The separately
authorized live Phase B completed 35 observations, promoted 29 of 30 candidates,
and rejected one. It saved 540 seconds, or 0.946% of matched all-F3 lifecycle time;
runtime-model error was 2.303%. This is a modest single-seed operational result.
The rejected candidate has no F3 counterfactual, and fixed restoration/restart/
warm-up costs limit the savings. Adaptive multi-fidelity superiority is not shown.

F4 tested four finalist configurations against default across four common-seed blocks
(20 observations). All finalists passed the frozen gates. Configuration E was selected
by the latency-first rule, with mean p99 25.952 ms, mean same-block TPS improvement
5.118%, and p99 reduction 3.389 ms. This is a repeated bounded configuration
recommendation, not a population-superiority or method-family ranking. The deployment
decision required exact application and rollback testing before separately authorizing
persistent activation. The closeout live status check found E active, settings verified,
and the target healthy with no pending restart, active campaign, managed index, or CHARM
session. No deployment-performance benchmark was added.

## Publication correction: retries and shared attempts

The closeout audit found that the frozen final safety table uses a total-attempt counter
as if it were a retry counter. It therefore marks all slots as retried. Its
safety figure also sums method attributions of shared BO initialization and
describes all retained attempts as non-training infrastructure attempts.
Successful first attempts are included in those totals, so that caption is wrong.

Use the generated [corrected safety
table](../../../results/thesis-final/safety-accounting-correction.csv) and [erratum with
replacement caption](../../../results/thesis-final/safety-accounting-erratum.md) when
interpreting the results. The audited physical totals are:

| Quantity | Correct value |
|---|---:|
| Physical observation slots | 655 |
| Total attempts, including first attempts | 657 |
| Completed attempts | 655 |
| Retained failed attempts | 2 |
| Slots requiring a retry | 2 |

Both failures affected shared BO initialization: `DATASET_RESTORE_FAILED` and
`HOST_POWER_INTERRUPTION`. Each BO method inherits two logical retry attributions;
these must not be summed to six physical events. Random, Sobol and controls had
zero retries. The original analysis/report, hashes and deployment evidence remain
unchanged. The correction is an explicit post-result reporting erratum; it changes
no performance estimate or statistical test. A report that reproduces exactly
can still contain a semantic reporting defect.

## Acceptance and exclusions

The decision was to close the bounded five-method study using the acceptance
summary below. Broader platform requirements remain outside that completed scope,
and failed historical gates retain their original interpretation.

| Requirement group | Closeout disposition | Source |
|---|---|---|
| Measurement profile and restoration | Passed for recorded context; duration pilot closed by conservative futility decision, not full planned execution | Frozen profile; candidate-restore, duration and reliability ledgers |
| Five methods, five seeds, equal logical budgets | Complete | Primary runs, attempts and lineage; final analysis |
| Screening and learned constraints | Exploratory screening amendment; honest no-learned-constraint fallback | Calibration and screening analysis; exploratory-use decision |
| Secondary multi-fidelity | Complete bounded retrospective/live demonstration | Phase-A and phase-B analyses |
| F4 and recoverable deployment | Complete for E, active by separate authorization | F4 and deployment ledgers; explicit activation authorization |
| Final report and evidence inventory | Integrity and rerender passed; use safety erratum | Closeout audit, corrected safety table and caption |
| Generality, calibration, coordination, transfer, adaptation | Unsupported by the final v2 comparison | Known limitations; legacy criteria retained |
| Dedicated 1-hour/24-hour reliability claims | Not established by this closeout | The reliability validation proves 15 consecutive restores; it is not a substitute for either named soak protocol |
| Independent reproduction | Not established by this closeout | Reproducibility guide |

No analytical/read-heavy/hidden workload generalization, learned safety calibration,
coordinated knob/index advantage, transfer benefit, drift-adaptation effectiveness, or
ablation conclusion is claimed. The historical 168-arm matrix and twelve ablations are
not completed by the narrowed v2 comparison. The existing literature review is a dated
scoped review; this audit adds no new literature search or novelty claim. See the
current [limitations](../../../docs/limitations.md) for the scope of supported claims.

## Evidence inventory and reproduction

The authoritative external evidence root is `C:\CHARMDB-ARTIFACTS\v2`. It is not
included in Git. The closeout audit streamed SHA-256 verification of all 5,457 manifest
entries (60,121,496,447 bytes), checked for unmanifested files, authenticated the final
analysis, and rerendered the 17-file final report in the workspace. All nine CSVs were
populated and all six SVGs parsed. This proves current-host offline report reproduction
and file integrity, not an independent clean-machine experiment run.

| Artifact | Relative path under evidence root | Payload SHA-256 or file SHA as labelled |
|---|---|---|
| Initial three-seed analysis | `primary-comparison/primary-analysis.json` | payload `fa6263830e5081a64943c1057083450076a32279a191bb889dc66944cbda8f21` |
| Standalone two-seed analysis | `primary-wave-b/primary-wave-b-analysis.json` | payload `c2486ee794d323af9a033d811e89329582469583980340b9bdbe15fa6db78ebb` |
| Final five-seed analysis | `primary-wave-b/final-five-seed-analysis.json` | payload `769f5a0eabdebe272a0a8ae4b7578fd54f72d4a95cd53822e1cb993466a36fb2` |
| Final report index | `primary-wave-b/report-final-five-seed/final-five-seed-report-index.json` | file `97a37d469ca515a4fb4889f6548c8c42ff33b85b1a41d6504dc2e4b551747fd2` |
| F4 analysis | `f4-confirmation/f4-analysis.json` | payload `00d67d24e07670a0d8cb11e754854189f99afb28f78ed240f2ab01b9b8b3230c` |
| Live multi-fidelity analysis | `multi-fidelity/phase-b/phase-b-analysis.json` | payload `1c997ba5d595d9fe0e73778313d249cc94ee9fca337738b51da5852e3efa65cc` |
| Deployment ledger | `evidence-ledgers/apply-best-deployments.json` | payload `814ae6c0a0e1ed993acf8bc7edc74ac7b7bad0e628000d1a31c1ed86b16fb833` |
| Tree manifest | `evidence-sha256-manifest.json` | file `8fcfee8eca196a966f86d16ab6e2b7ea91b3f4bfd401ac360dd7e11b7c807e96` |

The [machine audit](d072-evidence-audit.json) records counts, hashes and correction
rows. The final report copy and correction are generated under
`artifacts/reports/d072-closeout/`. Preserve the correction together with the report.
The [validation record](d072-validation.json) records the three passing regression
tests, source/lockfile hashes, Git HEAD caveat, and separate live target check.

From the repository, with the existing locked environment:

```powershell
.venv\Scripts\python.exe scripts/audit_thesis_closeout.py --root C:\CHARMDB-ARTIFACTS\v2 --output artifacts/reports/d072-closeout
.venv\Scripts\python.exe -m pytest tests/unit/publication/test_closeout_audit.py
.venv\Scripts\charmdb.exe apply-best-status --deployment-id c8e561f0-fce1-5c97-b2e8-d3a5bf3d84b2
```

The audit requires the exact post-activation evidence manifest hash; it fails on altered
source files, path escapes, unexpected files, or report differences. A copied evidence
root may be supplied with `--root`; hashing uses relative inventory paths. The script
writes only to the output directory, outside the source evidence tree, and does not
connect to PostgreSQL or run a benchmark. The third command is a separate live read-only
check. Its status is time-dependent and is not proven by an offline ledger.

To reconstruct a development environment, the repository's frozen dependency command is
`uv sync --extra dev --frozen`. The closeout did not execute this on a clean machine.
Use [the operator guide](../../../docs/operator-guide.md) for historical execution
commands; do not rerun campaign creation, restore, integration tests, or benchmark
commands on the active target as part of report reproduction.
