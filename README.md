# CHARM-DB

CHARM-DB is a PostgreSQL tuning research system with a durable control database, isolated target, candidate-level restores, five-method primary optimization, secondary multi-fidelity analysis, F4 confirmation, and a recoverable `apply-best` workflow. The thesis implementation in `src/charmdb` is the canonical runtime. Protocol identifiers containing `v2` remain frozen scientific and database contracts.

The recorded five-seed comparison and final report are complete with drift flags. The [frozen report](results/thesis-final/frozen-report/final-five-seed-report.md) is preserved; cite its [D072 safety correction](results/thesis-final/safety-accounting-erratum.md) for retry counts. Champion E was recovery-tested and explicitly activated on the recorded host. These records do not establish clean-machine replication, cross-workload tuning, learned-risk constraints, or index coordination efficacy.

## Start here

Install Python 3.12 or 3.13, `uv`, Docker Compose, PostgreSQL client tools, and `pgbench`. Copy `.env.example` to `.env` and set separate control and target DSNs and credentials. Keep the target on an allowlisted host and set `CHARMDB_ARTIFACT_DIR` to a separate evidence root with sufficient space. Run `make install`, `make up`, and `make smoke`. A new machine needs fresh control and artifact state and recaptured reference evidence.

The CLI is `uv run charmdb --help`. `make manifest-validate`, `make import-check`, `make lint`, `make typecheck`, `make unit-test`, and `make build` are local checks. `make integration-test` requires disposable separate `charmdb_test_*` databases and `CHARMDB_DISPOSABLE_INTEGRATION=1`; see [development](docs/development.md).

Read [installation](docs/installation.md), [configuration](docs/configuration.md), the [architecture](docs/architecture.md), [operator guide](docs/operator-guide.md), [methodology](docs/methodology.md), [reproducibility guide](docs/reproducibility.md), and [limitations](docs/limitations.md). Frozen manifests and preregistrations live in [`experiments/thesis`](experiments/thesis); historical materials live in [`docs/history`](docs/history) and [`experiments/historical`](experiments/historical). Authenticated raw measurements, snapshots, and control records stay in their artifact and database roots; the curated results are publication copies.
