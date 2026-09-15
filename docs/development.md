# Development and CI

`make install` synchronizes the frozen lock with development and documentation extras. `make lint`, `make typecheck`, `make unit-test`, `make import-check`, `make manifest-validate`, and `make build` cover canonical source. The import check parses source, tests, and scripts and rejects executable imports of removed historical modules. CI runs these static checks without a live target.

Integration tests are opt-in. Set `CHARMDB_DISPOSABLE_INTEGRATION=1` with separate control and target DSNs whose database names start with `charmdb_test_`, a `COMPOSE_PROJECT_NAME` starting with `charmdb_test_`, and an integration artifact root under `.pytest-tmp-integration`. The fixture refuses production-like DSNs or an `.env`-derived live connection. Only then run `make integration-test`; it can mutate disposable test state.

CI cannot prove Docker/PostgreSQL restoration, 600-second measurement validity, artifact-root health, or deployment recovery. Those require a dedicated disposable environment and operator workflow.
