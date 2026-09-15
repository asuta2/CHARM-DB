# Frozen benchmark profile

The benchmark decision freezes the following profile for the final default-reference
block, parameter screening, and later v2 stages. A later change requires a new
append-only decision and invalidates direct pooling with evidence collected under this
profile.

| Field | Frozen value |
|---|---|
| Profile label | `scale500-c32-w600-f3-600-v1` |
| Target | PostgreSQL 18.1 image `postgres@sha256:5773fe724c49c42a7a9ca70202e11e1dff21fb7235b335a73f39297d200b73a2` |
| Target resources | 4 CPUs, 4 GiB memory, 1 GiB shared memory, PID limit 512 |
| Preflight | `137d0027-c256-59fa-b583-4277e6461486` |
| Candidate baseline | `35459275-c9bf-544f-be54-4e3537464c78`, scale 500 / 50,000,000 accounts |
| Workload point | concurrency 32, four pgbench client threads |
| Warm-up | 600 seconds |
| F3 measurement | 600 seconds |
| Candidate reset | logical candidate restore before every measured evaluation, with the approved exact-core and physical-tier fingerprint checks |
| Activation | unconditional PostgreSQL restart before every evaluation |
| Maintenance | `canonical-baseline-only-no-vacuum`; timed warm-up and measurement use `--no-vacuum` |
| Objectives | maximize TPS and minimize p99 latency |
| Feasibility | execution validity and hard gates only; no 20 ms p99 SLO |
| Evidence root | Separate configured `CHARMDB_ARTIFACT_DIR` with stage-specific subdirectories |

## Duration decision and limitation

The duration campaign completed 12 of its planned 21 measurements with zero
benchmark failures. The decision was to stop it because 300 seconds had already
become mathematically unable to pass the pre-registered standalone p99 gate:
the two observed absolute p99 differences were 1.806 ms and 7.745 ms, and the
median of three values cannot fall below 1.806 ms even if the unrun third value
were 0 ms. The partial long-run results also showed second-half improvement in
9/10 TPS traces and 10/10 p99 traces. The conservative 600-second fallback is
therefore selected.

This is a futility decision, not a completed 18+3 analysis. The 12 completed
runs and nine deliberately unexecuted planned records remain durable. They may
be used to explain the selection and its limitations, but must not be reported
as a fully completed duration pilot.

## Still unresolved

The reference-design decision separately resolves the default-reference precision/drift
thresholds and five final seeds without changing this profile. The runtime review
accepts the bounded default-reference runtime. The validation record confirms that the
five-observation launch block passed without contingency. The primary hypervolume
reference point, screened knob set, learned-constraint framing, and primary budget and
schedule remain separate gates.