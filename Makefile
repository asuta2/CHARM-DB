.PHONY: install up down seed-scale10 smoke lint typecheck unit-test integration-test build manifest-validate frozen-input-check import-check primary-final-report apply-best-status

install:
	uv sync --extra dev --frozen

up:
	docker compose up -d --wait
	uv run charmdb migrate

down:
	docker compose down

seed-scale10:
	uv run charmdb seed --scale 10

smoke:
	uv run charmdb smoke

lint:
	uv run ruff check src tests scripts

typecheck:
	uv run mypy

unit-test:
	uv run pytest tests/unit

integration-test:
	uv run pytest tests/integration

build:
	uv build

manifest-validate:
	uv run python -m charmdb.protocol experiments/thesis/manifests

frozen-input-check:
	uv run python scripts/check_frozen_inputs.py

import-check:
	uv run python scripts/check_canonical_imports.py

primary-final-report:
	uv run charmdb primary-final-report $(WAVE_B_CAMPAIGN_ID)

apply-best-status:
	uv run charmdb apply-best-status --deployment-id $(DEPLOYMENT_ID)
