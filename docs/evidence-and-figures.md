# Evidence and figure specification

This specification is frozen before any primary observation exists. It defines
axes, aggregation, uncertainty, failure handling, inclusion, drift adjustment,
and the boundary between confirmatory Wave A and any later exploratory work.

## Required retained evidence

Each observation retains raw pgbench transaction logs; TPS and p50/p95/p99;
failures, timeouts, retries, gate stops, crashes, and rollback status; named and
normalized candidates; method, seed, slot, role, block, chronological index, and
matched default block; phase timings; restore ledger and baseline fingerprint;
resource telemetry; and environment, image, snapshot, code, and manifest IDs.

## Predefined tables

Provenance; final knob ranges; screening decisions; duration agreement; method
outcomes with uncertainty; failures and safety outcomes by method; budget and
wall-clock accounting; default drift; restore performance; and multi-fidelity
accuracy and modeled/measured savings.

The primary method table has one row per method and reports all three seeds,
valid/failed candidate counts, retryable infrastructure attempts, final
fixed-reference hypervolume, best observed TPS, minimum observed p99, mean
control-relative TPS, and mean control-relative p99. A second table reports all
ten pairwise method contrasts for each registered endpoint. The default is not
a method row and consumes no candidate slot.

## Predefined figures

TPS versus p99 and Pareto front; saturation curves; paired duration agreement;
rank and Pareto stability; best-so-far and fixed-reference hypervolume by slot;
per-seed and aggregate trajectories; default drift and reference bands; safety
outcomes; knob importance; phase timing; restore timing; resource profiles; and
multi-fidelity window/promotion diagnostics.

Historical diagnostic plots are labeled separately and never combined with v2
primary figures.

## Primary Wave A analysis rules

The independent unit is the frozen seed, not an individual observation. Each
method contributes 30 logical candidate slots per seed. The 12 physically
shared BO initialization observations are included once in each Bayesian
method's 30-slot trajectory and are never included in random or Sobol. The 18
adaptive observations for a Bayesian method train only on its own earlier
adaptive observations and the 12 shared observations from the same seed.

The registered endpoints are:

1. final hypervolume after slot 30 at the fixed raw-objective reference
   `(throughput_tps, negative_p99_ms) = (0,-40)`;
2. best observed TPS by slot 30;
3. minimum observed p99 by slot 30;
4. mean candidate TPS divided by the contemporaneous interpolated default TPS,
   minus one;
5. mean candidate p99 minus the contemporaneous interpolated default p99.

For each endpoint, report each seed value, the across-seed mean and median,
standard deviation, median absolute deviation, and a deterministic two-sided
95% percentile bootstrap interval with 10,000 resamples of whole seeds. With
only three seeds these intervals are descriptive and must not be described as
high-precision population intervals. Pairwise method comparisons use the
within-seed difference, exact two-sided sign-flip permutation p-value, paired
standardized effect when defined, and Cliff's delta. Holm correction is applied
across the ten method pairs separately within each endpoint family. The minimum
possible two-sided exact p-value at three paired seeds is coarse; effect sizes and all
seed values remain primary.

For the pre-registered combined Wave A + Wave B analysis, the same two-sided
test is retained at five paired seeds. Its unadjusted floor is `2/2^5 = 0.0625`,
not approximately 0.03; `1/2^5 = 0.03125` is the floor only for an unregistered
one-sided test. Do not switch tails after Wave A. P-values remain supplementary
to per-seed direction and practical effect size. The five endpoint families are
derived from TPS and p99, with hypervolume derived from the same pair, and must
not be presented as five statistically independent confirmations.

Controls are interpolated piecewise linearly within each seed using positions
1, 34, 66, 99, and 131. Values outside a bracketing interval use the nearest
endpoint control. Raw TPS/p99 and adjusted contrasts are both reported. A
least-squares fitted endpoint change is computed from all valid controls within
each seed; absolute TPS change above 5% of that seed's control mean or absolute
p99 change above 5 ms raises a transparency flag. It does not terminate or
discard Wave A. Missing controls are reported and prevent an adjusted contrast
where interpolation is impossible; they are never replaced by the historical
global default mean.

Only a completed workload with finite positive TPS/p99, zero transaction
failures, passed candidate restore/fingerprint checks, complete output, and
passed rollback/target-health gates is a valid objective observation. A
candidate-caused invalid, failed, timed-out, duplicate, or unsafe proposal
retains and consumes its method slot but does not enter GP training or numeric
endpoint calculations. `DATASET_RESTORE_FAILED` and
`BASELINE_FINGERPRINT_FAILED` are infrastructure failures: the identical
persisted proposal may receive at most three one-attempt trials, does not train
the optimizer, and consumes no additional candidate slot. Exhaustion pauses the
campaign and requires a documented human decision; it is not silently changed
into a method failure.

The primary Pareto plot uses TPS on the x-axis and p99 milliseconds on the
y-axis (lower is better), shows every valid candidate, distinguishes methods,
and overlays contemporaneous controls without joining them to a method. The
hypervolume plot uses logical candidate slot 1–30 on the x-axis and the frozen
raw-objective hypervolume on the y-axis, with thin per-seed lines and an
aggregate seed summary. Best-TPS and minimum-p99 trajectories use the same
slot axis. The drift figure uses chronological physical position 1–393 and
shows controls separately by seed; it must not imply that shared BO points were
measured three times.

Wave A is reported on its own before any optional Wave B pooling. No post-result
change to endpoints, reference point, valid-observation rules, retry treatment,
drift adjustment, or multiplicity family is permitted without an append-only
deviation record and a separate sensitivity analysis.

D063 changes Wave B from optional/reserved to authorized-after-readiness but
does not change those analysis rules. Render a standalone two-seed Wave B
report first, followed by a combined five-seed report that combines seed-level
endpoint values only. Candidate distributions and local-control domination
shares remain descriptive; candidate rows are not independent replicates.

D066 prospectively requires the combined five-seed report to regenerate the
complete primary surface after terminal Wave B: JSON analysis, Markdown, all
nine primary CSV tables, all six primary SVG figures, and the deterministic hash
index. The consolidated renderer must merge the detailed five-seed trajectories,
controls, Pareto rows, drift records, failures, and safety accounting needed by
those outputs; a summary-only merge is insufficient.

The combined outputs treat all five seeds as one cohort. They must contain no
Wave A/Wave B label, heading, facet, legend entry, color, marker, line style,
ordering, filename, subgroup statistic, or other visual/tabular distinction.
Method identity remains visible. Seed-level values and thin seed trajectories
remain available because seed is the independent unit, but the combined report
does not disclose a seed's wave membership. For the combined drift SVG, use the
common within-seed scheduled position 1-131 rather than concatenating waves on a
1-655 axis. Standalone Wave A and Wave B reports and machine-readable source
hashes retain the provenance needed for audit and do not alter this unified final
presentation.

Combined rendering is acceptance-complete only when a synthetic five-seed test
verifies nine non-placeholder CSVs, six valid SVGs, consistent row counts,
monotone trajectory invariants, exact analysis/table/file hashes, byte-identical
rerendering, and absence of Wave A/Wave B presentation tokens. Real values and
figures are generated only after all 262 Wave B observations are terminal and
the standalone Wave B report authenticates them.

D068-D069 close those gates. The standalone Wave B analysis/report was rendered
first under analysis SHA `c2486ee7...6db78ebb` and report payload SHA
`4b322d90...a329c6b`. The subsequent combined analysis SHA is
`769f5a0e...66a36fb2`. Its report contains 17 files: Markdown, nine populated
tables (25 controls, 5 drift rows, 5 method outcomes, 50 pairwise contrasts,
8 Pareto rows, 6 safety rows, 25 seed-level summaries, 25 seed-method outcomes,
and 750 trajectory rows), six SVGs, and the index. Table-payload SHA is
`5776d42c...1e02634` and index SHA is `97a37d46...51747fd2`. The real-data
rerender was byte-identical, every SVG parsed as XML, and the Markdown/CSV/SVG
scan returned zero Wave A/Wave B token matches. The combined drift figure uses
the common within-seed 1-131 schedule axis.

D070 adds a separate non-benchmark deployment ledger to the standard evidence
package. `apply-best-deployments.json` contains the singleton champion-E workflow,
source hashes, requested configuration and configuration hash, durable recovery
application/snapshot IDs, exact apply/rollback health evidence, the retained sandbox
permission interruption, and empty activation-authorization fields. Its payload SHA is
`0d41c0fd...cca31`. The refreshed 26-export index payload/file SHAs are
`0df2cbb8...92458` / `feca1ed5...ffb47`. The complete 5,457-file evidence-tree
manifest covers 60,121,494,326 bytes under payload/file SHAs
`7553180f...4bde6` / `e2e37f0a...58ea5`.

D071 refreshes the same deployment export with persisted explicit authorization,
`ACTIVE` state, activation application/snapshot, and passed setting/health checks.
Its payload SHA is `814ae6c0...16fb833`; the 26-export index payload/file SHAs are
`3af9b7ff...9bab0c3` / `22963142...8f42db`. D070 hashes above are historical.
No benchmark observation was added.

The results narrative leads with reliability of candidate quality: Wave A BO
methods held mean control-relative TPS near zero and locally dominated default
in 66.7%-68.9% of candidates, while random/Sobol lost about 7% mean TPS and
dominated in only 13.3%-16.7%. Raw best-TPS extremes do not establish that BO
finds a faster champion. Report p99 in milliseconds, state that about 1 ms at
about 26 ms is modest, and do not rank qLogNParEGO against qLogNEHVI; their
individual values stay visible under a family-level multi-objective conclusion.

The D039 pre-launch package must also export migration 038, the complete
primary restore-soak block/run ledger, every linked restore validation, duration
summary, init-process evidence, configured artifact directory, and result hash.
The evidence-tree manifest is rooted at the configured `CHARMDB_ARTIFACT_DIR`;
the retained OneDrive source copy is not the authoritative runtime tree after
relocation.

## D040 implemented reporting surface

The specification above is now executable rather than descriptive. `charmdb
primary-report <campaign_id>` renders a complete package from a terminal
analyzed block, confined beneath the configured artifact root:

- `primary-report.md` with the method-outcome table, seed-level endpoint
  summaries, all ten pairwise contrasts per endpoint family, default-control
  drift, safety and failure accounting, the nondominated candidate list, and the
  mandatory inference guards;
- `tables/*.csv` for method outcomes, seed-method outcomes, seed-level
  statistics, pairwise contrasts, safety and failures, default controls, default
  drift, the nondominated front, and long-form slot trajectories;
- `figures/*.svg` for TPS versus p99 with the front and overlaid controls,
  fixed-reference hypervolume by logical slot, best-throughput by slot, minimum
  p99 by slot, default-control drift over chronological physical position 1-393,
  and terminal slot outcomes by method;
- `primary-report-index.json` carrying the canonical payload SHA-256 of the
  rendered tables and a separate SHA-256 for every written file.

Rendering is deterministic: it contains no timestamp, no locale-dependent
formatting, and no randomness, so re-rendering the same analysis reproduces
byte-identical files. All output is UTF-8 without a byte-order mark.

The analysis payload now carries the per-seed, per-method 30-slot logical
trajectory required by the figures. Slots 1-12 of each Bayesian method read the
12 shared initialization observations that were physically measured once; slots
13-30 read that method's own adaptive observations; random and Sobol read their
own 30. A slot whose observation is invalid still consumes its budget and
carries the previous best-so-far value forward. The method table now also
reports candidate-failed slots, completed-but-invalid slots,
infrastructure-exhausted slots, and retained infrastructure attempts.

Because these outputs cannot be tested against a campaign that has not run,
`charmdb primary-analysis-rehearsal` executes the entire path on three
synthetic 393-slot ledgers - nominal, failures-and-retries, and drift-flagged -
and checks structural invariants: contiguous 1-30 budgets, correct shared-
initialization attribution, monotone hypervolume and best-so-far trajectories,
ten pairs in each of the five endpoint families, Holm adjustment never reducing
a p-value, one drift record per seed with `campaign_killing` false, five
controls per seed, and terminal slot accounting summing to each method's logical
budget. The synthetic values come from a documented SHA-256 pseudo-random
formula, never from a benchmark. Every rehearsal artifact is labelled
`synthetic`, carries evidence role `INFRASTRUCTURE`, and is written under
`primary-comparison/analysis-rehearsal-synthetic/`. It is not evidence about
any method.

## Registered secondary showcase displays (D050)

Wave A's terminal analysis is complete (D049, outcome `COMPLETE_WITH_DRIFT_FLAGS`,
analysis payload SHA-256 `fa6263830e...cbda8f21`). The following secondary
displays were adjudicated against the full three-seed results and are registered
here with frozen formulas before implementation. They are exploratory
presentation aids: every one reuses already-registered quantities, none defines
a new endpoint, none participates in confirmatory inference, and the five
registered endpoints plus the D049 deterministic report remain the sole primary
evidence. Seed is the only inferential unit; candidate-level rows are
descriptive and are never treated as independent replicates. No composite
"overall winner" score may be constructed.

- **S1 — local-control delta quadrant plot.** Every valid candidate at
  x = TPS/interpolated contemporaneous control TPS − 1 and
  y = p99 − interpolated contemporaneous control p99 (exactly the endpoint-4/5
  contrast formulas), zero lines dividing four quadrants, with adjacent
  per-method stacked bars of the four outcome shares (better-both, TPS-only,
  p99-only, worse-both) taken from the terminal `seed_method_results` counts.
  Rendered per seed or seed-marked; quadrant shares are never pooled for
  inference. Three-seed basis: 19–21/30 BO candidates dominate their
  contemporaneous default in every seed versus 2–6/30 for random/Sobol.
- **S2 — shared-initialization versus adaptive-gain decomposition.** For all
  five methods and each seed: hypervolume at slot 12 and slot 30, absolute and
  percentage gain, and best-TPS/minimum-p99 changes, from the frozen slot
  trajectories. Random and Sobol show their own slot-12-to-30 change for
  symmetry. The display must not claim that only qLogNParEGO improves after the
  shared design: three-seed gains are qLogNParEGO +737.0/+1303.5/+1583.7,
  qLogNEHVI +2.3/+1535.8/+1583.2, qLogNEI +0.8/+1513.6/+294.4, Sobol
  0/+942.8/+829.4, random exactly 0 in all three seeds.
- **S3 — annotated per-seed Pareto decision panels.** One panel per seed built
  on that seed's physical nondominated set, annotating physical method,
  within-seed slot, TPS, p99, control-relative deltas, and the normalized
  eight-knob configuration. Only the formula-free extremes (maximum TPS,
  minimum p99) may carry tags; a knee tag is prohibited unless explicitly
  labeled post-hoc, because no knee formula was frozen before seeds 2–3 were
  inspected. The pooled cross-seed front conflates drift states and may appear
  only with an explicit pooling caveat. Three-seed basis: seed-1's front is
  qLogNParEGO ×2 plus a Sobol throughput extreme; seed-2's and seed-3's fronts
  contain only BO/shared-initialization points.
- **S4 — cross-seed consistency heatmap and leave-one-seed-out table.** Method ×
  seed heatmap of final hypervolume, best TPS, minimum p99, both mean
  control-relative endpoints, and local-control domination rate, showing values
  and within-seed ranks, plus mean rankings for all-seeds and each single-seed
  omission. Three-seed basis: the BO-above-baseline ordering survives every
  omission on hypervolume, minimum p99, and both control-relative endpoints;
  the qLogNParEGO-versus-qLogNEHVI ordering flips with seed-1 inclusion, so
  only the family-level multi-objective claim is presented as stable.
- **S5 — control-relative candidate distributions.** Per-method ECDF or jittered
  distributions of the candidate-level control-relative TPS and p99 contrasts,
  seed-grouped with seed-mean markers and a zero reference line, labeled
  descriptive-only. Three-seed basis: median control-relative TPS −8.3% to
  −11.1% for random/Sobol in every seed versus +1.9% to +4.3% for BO; median
  p99 deltas −1.0 to −2.1 ms for BO versus −0.05 to +1.3 ms for the baselines.
- **S6 — search-diversity versus outcome (appendix/explanatory only).** Median
  nearest-neighbour Euclidean spacing of each method's 30 normalized candidate
  vectors against domination rate and final hypervolume. Presented as an
  exploration–exploitation illustration, never as an effectiveness endpoint.
  Three-seed basis: Sobol spans 0.712–0.722 in every seed; BO concentrates
  (e.g. qLogNParEGO 0.404–0.655) while achieving better drift-adjusted outcomes.
- **S7 — wall-clock efficiency curve: rejected as a standalone figure.**
  Measured per-slot wall cost is method-flat: with the 12 shared initialization
  observations (≈5.5 h/seed) allocated once physically, all method totals fall
  in 13.74–13.97 hours per seed and per-cell restore medians span only
  437.5–452.5 s, so a wall-clock axis is a near-linear rescaling of the slot
  axis. Its content is reduced to a cost-accounting row in S8. Shared
  initialization cost is either shown separately or allocated identically to
  all three BO methods; the same physical observations are never counted three
  times in a total-cost claim.
- **S8 — compact summary dashboard.** One row per method: final hypervolume,
  slot-12-to-30 gain, best TPS, minimum p99, local-control domination share,
  per-seed front contributions, candidate failures and retained infrastructure
  retries, and physical wall hours (with the shared-initialization accounting
  rule stated inline). Values only — no composite score, no ranking aggregation
  beyond what S4 already registers.

Unfavorable results remain mandatory content wherever relevant: the baselines'
raw-TPS extremes, BO's throughput parity (not dominance) against the local
default, and the seed-88408573 drift flag. The adjudication computation and its
output are retained at
`primary-comparison/secondary-showcases/showcase-adjudication.py` and
`showcase-adjudication-results.json` (SHA-256 `4b70209...9bd7dac`,
`33bc60f...c061d930`), derived exclusively from the terminal analysis export.

D051 implements this registration in `charmdb.reporting.secondary` and the
`primary-report-secondary` CLI command. The renderer authenticates the
terminal export's embedded analysis hash and requires its complete 393-slot
history before writing. The executed output is
`primary-comparison/report-secondary/`: nine CSV tables, eight SVG displays,
one Markdown report, and one JSON hash index (19 files / 235,852 bytes; index
SHA-256 `b3f0f6c...a7aabc`; canonical table-payload SHA-256
`27af898...fea20`). A second real-data render was byte-identical. The index
declares `EXPLORATORY`, seed as the inferential unit, candidate rows as
non-inferential, no endpoint change, and S7 as rejected. The refreshed 3,319-file
evidence-tree manifest has file SHA-256 `38880f57...e4e3044` and payload
SHA-256 `7e996972...e2cc93`.

## Frozen screening evidence

The screening report must show all 32 planned Sobol slots, including invalid or
failed outcomes; the three chronological default controls; PRCC values for TPS
and p99; the maximum-absolute statistic and 0.20 threshold; ambiguity and any
paired endpoint follow-ups; control drift; final inclusion/exclusion rationale;
and the exact manifest, Sobol-design, and schedule hashes. It must state the
valid-observation count and must not silently replace a failed configuration.
Migration 032 provides the immutable block/run source for this report; terminal
analysis exports must verify their embedded ledger SHA-256 before writing.
If D031 supplies positions 34 and 35, the report must also identify both source
ledgers, show D030's failed/planned tail, include the three-repetition restore-
stability result hash, and expose the unadjusted chronology gap. It must not
present the two ledgers as one uninterrupted execution block.
D032 evidence must additionally retain D031's terminal fingerprint mismatch,
the registered orphan file nodes and byte totals, pre/post database size, the
snapshot-backed remediation result hash, and the superseding validation lineage.
D033's terminal report must show the 6.438 ms p99 drift failure prominently and
must not itself display a selected knob set or imply that OAT was run. The
separate D035 amendment report may show the eight selected knobs only when it
also labels the selection post-result, exploratory, drift-limited, and without
OAT. It must give both included and excluded parameters, raw and time-adjusted
ranking checks, leave-one-out threshold frequency, and the exact D033 hash.
D034 has no result report: it was retired with zero campaign and zero
observations. Its manifest and migration remain as historical design evidence.

Primary Wave A reporting must remain separate from any later Wave B pooled
analysis. Report all three seeds, 30-slot method budgets, shared-initial-design
accounting, five controls per seed, failures/retries, and the fixed `(0,-40)`
hypervolume reference. Do not describe three seeds as five-seed confirmation.

`analysis_sha256` fields are SHA-256 hashes of canonical JSON analysis payloads,
not byte hashes of pretty-printed export files. Every export records both the
payload hash and a separate file SHA-256. Tree-level evidence manifests hash
each retained file independently and exclude the manifest file itself.

## Live multi-fidelity Phase B terminal evidence

D057 adds a compact authenticated Phase B analysis at
`multi-fidelity/phase-b/phase-b-analysis.json`. It reports the frozen success gates,
35-slot / 36-attempt accounting, promotion and rejection counts, matched all-F3
counterfactual time, pre-retry runtime-model error, control sequence, continuation
reconnect gaps, and the sole rejected candidate. Analysis payload SHA-256 is
`1c997ba5d595d9fe0e73778313d249cc94ee9fca337738b51da5852e3efa65cc`;
export file SHA-256 is
`cb5bd6e644fe89bd021ff44c53182dabdcc874f93a7f03bcd3dbae21052ce2b3`.

The evidence-ledger index also contains separate Phase B block, run, and attempt
exports with 1, 35, and 36 rows. Any thesis table must show that 29/30 candidates
promoted, one rejection saved 540 seconds / 0.946% of total matched lifecycle,
and the time-model error was 2.303% against the 10% limit. It must also retain the
D056 infrastructure failure, the five-control TPS/p99 sequence, the single-seed
scope, and the fact that candidate 26 has no observed F3 counterfactual. No figure
or table may label the rejection a true or false rejection, infer a population
promotion rate, or convert this secondary live demonstration into confirmatory
multi-seed evidence.

## D072 post-result safety-accounting erratum

The source specification and frozen reports remain unchanged. Closeout validation
found that the final safety table counts first attempts as retries and the safety
figure sums shared BO method attributions as physical infrastructure events. Use
the correction in `thesis-closeout.md` and its generated CSV/caption: 655 physical
slots, 657 attempts, 655 completed attempts, two failed attempts, two retried slots.
Each BO method inherits both shared-initialization failures; method attributions
must not be summed. No endpoint or inference changed. The D072 script reproduces
the original report exactly and emits the companion correction separately.
