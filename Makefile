.PHONY: bootstrap up down seed index-seed index-experiment fidelity-pilot calibration-pilot smoke-test test unit-test integration-test e2e-test benchmark-default tune-single tune-multi tune-coordinated evaluate evaluate-drift evaluate-ablation report soak-test

bootstrap:
	uv sync --extra dev --frozen

up:
	docker compose up -d --wait
	uv run charmdb migrate

down:
	docker compose down

seed:
	uv run charmdb seed --scale 10

index-seed:
	uv run charmdb index-seed --rows 300000

index-experiment:
	uv run charmdb index-experiment --customer-id 4242

fidelity-pilot:
	uv run charmdb fidelity-pilot

calibration-pilot:
	uv run charmdb calibration-pilot

smoke-test:
	uv run charmdb smoke

test: unit-test

unit-test:
	uv run pytest -m "not integration"

integration-test:
	uv run pytest -m integration

e2e-test:
	uv run pytest -m integration tests/integration/test_slice1.py

benchmark-default:
	uv run charmdb benchmark-default

tune-single:
	uv run charmdb tune-single

tune-multi:
	uv run charmdb tune-multi

tune-coordinated:
	uv run charmdb tune-coordinated

evaluate:
	uv run charmdb evaluate

evaluate-drift:
	uv run charmdb evaluate-drift

evaluate-ablation:
	uv run charmdb evaluate-ablation

report:
	uv run charmdb report

soak-test:
	uv run charmdb soak-test --duration "$(DURATION)"
