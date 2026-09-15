# CHARM-DB

### Database Optimization via Machine Learning

**Ajdin Šuta — Master’s thesis, Faculty of Electrical Engineering, University of Sarajevo, 2026.**

CHARM-DB is the research implementation and experiment repository accompanying this thesis. It studies how machine learning can allocate a limited PostgreSQL tuning budget to improve throughput and tail latency while preserving database durability and operational safety.

## Research overview

The study compares **random search, scrambled Sobol search, and three Bayesian optimization methods**: throughput-oriented qLogNEI, multi-objective qLogNParEGO, and multi-objective qLogNEHVI. The framework provides:

- Candidate-level database restoration and fingerprint verification for repeatable evaluations.
- Durable experiment scheduling, bounded retries, and interleaved PostgreSQL-default controls.
- Multi-fidelity evaluation using shorter measurement windows, finalist confirmation, and recoverable configuration deployment.
- Deterministic reports, publication figures, and evidence integrity checks.

The final comparison covers **five seeds, 30 candidate slots per method per seed, and 655 physical observations**, including shared initialization and default controls. It uses PostgreSQL 18.1, eight tuning parameters, and a fixed pgbench workload. See the [methodology](docs/methodology.md) and [benchmark profile](docs/frozen-benchmark-profile.md).

## Results and thesis materials

Bayesian methods produced configurations that jointly improved on the local default in approximately **63–68% of candidate slots**, compared with **13–19%** for random and Sobol search. Their mean throughput stayed closer to default while mean p99 latency improved. These are descriptive results for the tested workload; the five-seed comparison retains drift flags and does not establish universal performance superiority.

| Material | Location |
|---|---|
| Main findings and interpretation | [Results and discussion](results/thesis-final/thesis-results-discussion.md) |
| Complete five-seed report | [Frozen report](results/thesis-final/frozen-report/final-five-seed-report.md) |
| Publication-ready figures | [SVG figures](results/thesis-final/publication-figures) |
| Detailed measurements and comparisons | [CSV tables](results/thesis-final/frozen-report/tables) |
| Corrected failure and retry accounting | [Safety erratum](results/thesis-final/safety-accounting-erratum.md) and [corrected table](results/thesis-final/safety-accounting-correction.csv) |
| Experiment definitions and provenance | [Manifests, preregistrations, and receipts](experiments/thesis) |

Use the safety erratum when citing retry counts; the original report is retained for provenance. Raw transaction logs, database snapshots, and the full evidence archive are stored outside Git. The [reproducibility guide](docs/reproducibility.md) explains what can be verified from this checkout and what requires the original evidence.

## Getting started

Requirements: **Python 3.12 or 3.13**, `uv`, Docker Compose, and PostgreSQL client tools, including `pgbench`. Run from a source checkout:

```sh
uv sync --extra dev --frozen
uv run charmdb --help
```

For a local database environment, copy `.env.example` to `.env`, configure separate control and target databases, and choose a dedicated `CHARMDB_ARTIFACT_DIR`. Then, with Make installed:

```sh
make up
make smoke
```

Follow [installation](docs/installation.md), [configuration](docs/configuration.md), and the [operator guide](docs/operator-guide.md) before running experiments. A new host requires fresh state and reference measurements; independent clean-machine reproduction has not been established.

## Repository and development

- [`src/charmdb`](src/charmdb) — canonical runtime, optimization, restoration, analysis, reporting, and CLI.
- [`docs`](docs) — architecture, methodology, operation, and research limitations.
- [`experiments/thesis`](experiments/thesis) and [`results/thesis-final`](results/thesis-final) — experiment contracts and curated results.
- [`tests`](tests), [`migrations`](migrations), and [`scripts`](scripts) — validation, database schema, and supporting tools.

Run local checks with `make lint typecheck unit-test import-check manifest-validate frozen-input-check build`. Integration tests require disposable databases; see [development and CI](docs/development.md). Remaining research boundaries are documented in [limitations](docs/limitations.md).
