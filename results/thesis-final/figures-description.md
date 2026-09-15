# Selective final thesis figures

This post-result presentation revision implements the six figures selected after
review of the completed five-seed results. It changes no endpoint, inference,
observation, or original evidence export. SVG is the publication format; the
HTML gallery provides a convenient preview.

From the repository root, using the locked environment:

```powershell
.venv\Scripts\python.exe -B scripts/build_thesis_figures.py --root C:\CHARMDB-ARTIFACTS\v2 --output artifacts/reports/final-thesis-figures
```

The output directory must be new or empty and outside the evidence tree. For a
second reproduction, use a different output directory. Neither a database
connection nor an additional benchmark is needed.

| File | Content | Placement |
|---|---|---|
| `01-candidate-quality.svg` | All five seed values for adjusted TPS, adjusted p99, domination share and best TPS; means marked | Results |
| `02-initialization-and-adaptation.svg` | Fixed-reference hypervolume trajectories plus initial/later candidate-quality contrasts | Results |
| `03-default-drift.svg` | Five uniquely colored and dashed control series; flagged seeds marked | Results |
| `04-fidelity-and-lifecycle.svg` | Three-seed retrospective ordering, separate live promotion counts, primary lifecycle composition and recorded live savings | Discussion |
| `05-finalist-confirmation.svg` | Four-block F4 matched-default contrasts and frozen mean gates | F4 Results |
| `06-per-seed-pareto.svg` | Five physical frontiers on common axes, with point IDs linking to configurations | Appendix |

`existing-primary/` contains refreshed copies of the five usable original primary
figures. Method colors stay consistent. Seed colors do not cycle after the third
seed; patterns and symbols add redundant distinctions. Shared initialization is
counted once physically and remains shared in logical BO trajectories. The
historical safety chart is excluded because its attempt/retry interpretation is
incorrect; the README retains the corrected physical accounting.

The generator verifies all six source-analysis files and all 655 primary trial
JSON files against the source manifest, checks the final canonical analysis hash,
reconstructs and cross-checks the 750 logical control contrasts, and exports
supporting CSVs. `figure-index.json` records source hashes, renderer hash, output
hashes, row counts and palette mappings. Outputs are deterministic.

Scientific boundaries: the independent primary unit is seed; candidate fractions
are descriptive. Initial/later contrasts are chronologically confounded and do
not establish a causal stage effect. Retrospective fidelity covers three seeds,
live fidelity covers one, and F4 covers four blocks. The rejected live candidate
has no observed F3 outcome. Matched all-F3 timing is existing accounting, not a
new executed comparison. Lifecycle composition includes successful trials only;
its residual includes warm-up and other work, not measured optimizer overhead.

Historical report bytes remain reproducible with
`render_primary_report(..., legacy_drift_colors=True)`. The closeout audit uses
that explicit compatibility option. Current figure generation defaults to the
corrected palette; archived reports and their hash indexes are not overwritten.

Validation includes five unique seed colors and dash patterns, block-specific
F4 pairing, source-tampering rejection, output-path protection, existing reporting
regressions, real-data deterministic rerendering and visual SVG inspection.
