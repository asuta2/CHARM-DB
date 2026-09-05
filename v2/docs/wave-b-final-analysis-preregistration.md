# Wave B and final five-seed analysis pre-registration

## Status and timing

This record is frozen on 2026-09-05, before any Wave B campaign, trial,
attempt, candidate outcome, or benchmark output exists. D063 authorizes Wave B
implementation and later external execution after the durable runner,
result-blind analysis, tests, and clean-target readiness gates pass. The
approximately 121.010-hour benchmark is an external operator boundary and is
not launched as part of this documentation change.

Wave A has already been executed, authenticated, analyzed, and reported on its
own. Its results motivated the decision to spend the reserved confirmation
budget and the reliability-centered thesis emphasis, but they are not used to
change the benchmark profile, search space, methods, candidate budgets, seed
identities, endpoint formulas, reference point, validity rules, retry treatment,
drift adjustment, or multiplicity families.

## Frozen Wave B execution contract

- Use exactly the two untouched D025 seeds `1902413987` and `740267717`.
- Preserve 30 logical candidate slots per method and 131 physical observations
  per seed: 30 random, 30 Sobol, 12 shared BO-initial observations, 18 adaptive
  observations for each of qLogNEI, qLogNParEGO, and qLogNEHVI, plus five
  PostgreSQL-default controls at within-seed positions 1, 34, 66, 99, and 131.
- Preserve the eight-knob D035 space, scale 500, concurrency 32, four client
  threads, logical restore before every physical observation, unconditional
  PostgreSQL restart, 600-second warm-up, 600-second measurement, and the
  baseline-only/no-vacuum maintenance policy.
- Preserve same-seed isolated BO training, the shared 12-point initialization,
  equal logical method budgets, hard safety/validity gates, and the bounded
  three-attempt infrastructure-only retry rule. A retry reuses the immutable
  candidate and seed, consumes no new slot, and never trains an optimizer.
- Preserve temporally interleaved rounds and derive Wave B's deterministic
  schedule and fixed random/Sobol/shared-BO candidates before campaign creation.
  Their complete payloads and SHA-256 hashes must be committed and validated
  before any live observation starts.
- Create exactly one restart-safe Wave B campaign after readiness passes. Never
  replace it because of an unfavorable outcome.

Wave B therefore contains 262 additional physical observations. No F4 seed,
multi-fidelity Phase B seed, or replacement seed may enter this comparison.

## Frozen reporting order and pooling rule

1. Authenticate, analyze, and report the two Wave B seeds as a standalone
   confirmation result before opening the combined analysis.
2. Then combine the three already reported Wave A seed-level results with the
   two Wave B seed-level results, yielding exactly five independent seed values
   per method and endpoint.
3. In this document, "pool five seeds" means combining those seed-level
   summaries under the unchanged formulas. It does not mean treating the 655
   physical observations or candidate rows as independent replicates, and it
   does not permit a pooled cross-seed Pareto front to substitute for per-seed
   analysis.
4. Report Wave A, Wave B, and the combined five-seed analysis in separate,
   clearly labeled tables or sections. Any Wave A/Wave B disagreement remains
   visible.

## Endpoints and statistical interpretation

The five registered endpoints remain unchanged:

1. final valid-candidate hypervolume after logical slot 30 at
   `(throughput_tps, negative_p99_ms) = (0,-40)`;
2. best observed TPS by slot 30;
3. minimum observed p99 latency by slot 30, reported in milliseconds;
4. mean candidate TPS divided by the contemporaneous interpolated default TPS,
   minus one;
5. mean candidate p99 minus the contemporaneous interpolated default p99,
   reported in milliseconds.

Seed remains the independent unit. For every endpoint, retain the registered
seed values, mean, median, standard deviation, median absolute deviation,
whole-seed descriptive bootstrap interval, exact two-sided paired sign-flip
permutation comparison, paired effect, Cliff's delta, and Holm adjustment over
the ten method pairs within that endpoint family.

The test is not changed to one-sided after Wave A. With five paired seeds, the
smallest attainable unadjusted two-sided exact p-value is `2/2^5 = 0.0625`;
`1/2^5 = 0.03125` applies only to a one-sided directional test that is not part
of this protocol. P-values are therefore supplementary. The main evidence is
the magnitude and direction of the five seed-level effects and whether the
method-family separation repeats in every seed.

The five endpoints are five views of the same two measured quantities, TPS and
p99. Hypervolume is derived from them. Endpoint wins must not be counted as five
independent confirmations or accumulated as if they were independent studies.

## Frozen thesis claims

The primary contribution is reliability of budget use, not a raw-speed win.
Wave A showed that the best configurations were practically close and that
Sobol could supply the highest raw-TPS extreme. The clear separation was in
candidate quality: Bayesian methods averaged approximately -0.26% to -0.49%
control-relative TPS, while random and Sobol averaged -7.28% and -7.38%.
Bayesian candidates dominated their contemporaneous default on both objectives
in 60/90 to 62/90 cases (66.7%-68.9%), versus 12/90 and 15/90 (13.3%-16.7%) for
random and Sobol. Wave B tests whether this direction repeats in the two
reserved seeds.

Accordingly, the defensible main claim is that BO concentrates a fixed budget
in good regions and avoids the degradation caused by unguided exploration in
this experiment. Do not replace it with the broader claim that BO necessarily
finds a meaningfully faster PostgreSQL configuration.

Report p99 in milliseconds and include scale context. A difference of about
1 ms around a 26 ms p99 is consistent but modest (roughly 4%), and must be
described that way.

Report qLogNParEGO and qLogNEHVI individually, but do not rank them or declare
an internal winner. Wave A leave-one-seed-out ordering changed with seed 1.
The stable interpretation is at family level: the multi-objective approaches
lead the hypervolume and minimum-p99 endpoints, while their internal ordering
is not stable at the available seed count. F4's configuration-level champion
`E` does not convert into a method-level qLogNEHVI-over-qLogNParEGO claim.

## Multi-fidelity and F4 disposition

The multi-fidelity result is a bounded negative efficiency finding, not a failed
experiment. Under logical restore, unconditional restart, and a 600-second
warm-up, shortening only the measurement window saved 0.946% in the live
demonstration because the fixed lifecycle cost was still paid. Multi-fidelity
would become materially useful only if a fidelity level also reduced a fixed
cost, for example through a validated shorter warm-up or smaller scale. This is
a discussion/future-work point, not a request for another experiment.

F4 is complete. Its latency-first selection rule was frozen before F4 outcomes,
four common-seed restored blocks were run with local defaults, and treatment
`E` was selected. This addresses the concern that the configuration champion
could be an artifact of one F4 seed; it does not rank the optimizer families.

## Scope and limitations

All primary and F4 findings are conditional on one PostgreSQL OLTP workload:
pgbench's TPC-B-like transaction mix at scale 500 and concurrency 32 on the
recorded host/container environment. They must not be generalized to
analytical, mixed, or read-heavy workloads, other scales/concurrency levels,
other hardware, or other database engines. Cross-workload and cross-scale
validation belongs to future work.

## Apply-best boundary

Apply-best is not part of Wave B inference and must not alter the target before
Wave B is complete. After the final five-seed report, a separate v2 apply-best
workflow may activate F4 champion `E` only if it first provides a recoverable
configuration snapshot, exact requested/active-setting verification, restart
and health checks, an auditable rollback command, and a bounded operator time
estimate. The existing generic apply/rollback primitives are not by themselves
a thesis-safe apply-best workflow. No persistent activation occurs under D063.
