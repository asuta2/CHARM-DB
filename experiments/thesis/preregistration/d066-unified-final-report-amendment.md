# D066 unified final five-seed report amendment

## Status and timing

This prospective, append-only reporting amendment is recorded on 2026-09-09
while the sole Wave B campaign still has zero observations. The hash-frozen D063
pre-registration remains byte-identical and authoritative for execution,
authentication, endpoints, validity, adjustment, inference, and reporting order.
D066 changes only the publication surface of the final combined report.

## Required combined package

After the required standalone Wave B analysis and report have authenticated the
two new seeds, the final five-seed renderer must generate the complete primary
reporting package from the consolidated evidence:

- canonical JSON analysis and Markdown report;
- nine populated CSV tables;
- six deterministic SVG figures; and
- a deterministic index containing the analysis, table-payload, and per-file
  hashes.

The six figures are TPS versus p99/Pareto, hypervolume by logical slot,
best-throughput by logical slot, minimum-p99 by logical slot, default-control
drift by within-seed scheduled position, and safety outcomes by method. The
renderer must merge the five-seed trajectories, controls, Pareto rows, drift
records, failures, and safety accounting required by these outputs. A
summary-only endpoint merge is not the completed publication package.

## Unified presentation rule

The final combined package treats the five seeds as one cohort. Wave A and Wave
B must not be encoded or exposed as comparison groups in a combined table or
figure: no wave labels, headings, facets, legends, colors, line styles, marker
shapes, ordering, filenames, or wave-specific summaries.

Methods may remain visually distinguished. Seed-level values and trajectories
remain visible where required by the frozen seed-as-independent-unit analysis,
but a seed is not identified as belonging to Wave A or Wave B. For the combined
drift SVG, use the common within-seed scheduled position 1-131 rather than a
concatenated wave axis. Wave/source identity remains only in the standalone
reports and machine-readable provenance required to authenticate the merge.

The D063 requirement to render Wave A and Wave B standalone reports remains an
audit prerequisite. Any disagreement discovered during standalone confirmation
remains discussable in the audit narrative, but it is not encoded as a wave
split in the consolidated final tables or SVGs.

## Invariants and acceptance

D066 changes no benchmark slot, seed, candidate, method, endpoint, reference
point, validity rule, retry treatment, drift adjustment, multiplicity family,
or inferential formula. No placeholder value or figure is permitted.

Combined rendering is complete only when a synthetic five-seed test verifies
nine non-placeholder CSVs, six valid SVGs, consistent row counts, monotone
trajectory invariants, exact analysis/table/file hashes, byte-identical
rerendering, and absence of Wave A/Wave B presentation tokens. Real values and
figures are generated only after all 262 Wave B observations are terminal and
the standalone Wave B report authenticates them.

## Completion record

D068-D069 completed the required ordering on 2026-09-13 without changing this
prospective contract. Wave B was first authenticated and reported alone under
analysis SHA `c2486ee7...6db78ebb`. The unified five-seed analysis then completed
under SHA `769f5a0e...66a36fb2` and rendered nine populated CSV tables, six
parse-valid SVG figures, Markdown, and a deterministic index. Table-payload SHA
is `5776d42c...1e02634` and index SHA is `97a37d46...51747fd2`. A second
real-data render was byte-identical, and the combined Markdown/CSV/SVG scan found
zero Wave A/Wave B presentation tokens. Source-wave identity remains only in the
standalone audit packages and machine-readable provenance.
