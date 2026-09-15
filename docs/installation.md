# Installation

Use a source checkout: the Docker Compose project, target initialization SQL, forward-only migrations, and frozen protocol files are repository inputs rather than embedded wheel resources. Install Python 3.12 or 3.13, `uv`, Docker and Docker Compose, and PostgreSQL client tools (`pgbench`, `pg_dump`, `pg_restore`, and `psql`). On Windows, historical report/document PowerShell scripts may also need Microsoft Word/COM and Windows fonts; they are optional to canonical runtime.

From the checkout run `make install`, create `.env` from `.env.example`, then `make up` and `make smoke`. `make up` starts the pinned target and applies migrations to the separate control database. Installation alone never starts an experiment. Use `uv run charmdb --help` for commands and [configuration](configuration.md) for the safety settings. A standalone wheel provides the Python/CLI package, but the Docker-backed workflow still requires the checkout's external files.
