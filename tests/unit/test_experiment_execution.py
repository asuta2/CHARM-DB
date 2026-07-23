from typing import Any

import pytest

from charmdb.experiment_execution import (
    MEASURED_F3_TERMINAL_STATES,
    canonical_manifest_sha256,
    decide_prerequisite_gate,
    f3_equivalent_unit_value,
    validate_execution_definition,
)


def execution_definition(kind: str = "F3_EQUIVALENT_WALL_CLOCK") -> dict[str, Any]:
    accounting: dict[str, Any] = {
        "kind": kind,
        "relative_tolerance": 0.01,
        "f3_reference_wall_clock_seconds": 600.0,
    }
    if kind == "DRIFT_PHASE_UNITS":
        accounting = {
            "kind": kind,
            "relative_tolerance": 0.0,
            "phase_targets": {"pre_drift": 30, "recovery": 30},
        }
    return {
        "dataset": {"snapshot": "fixture-v1", "scale": 10, "schema_sha256": "a" * 64},
        "workload_contexts": [{"label": "transactional", "version": 1}],
        "execution_profile": {
            "warmup_seconds": 120,
            "measurement_seconds": 600,
            "concurrency": 4,
            "failure_limit": 5,
        },
        "search_space": {
            "version": "reload-knobs-v1",
            "parameters": {
                "random_page_cost": {"type": "continuous", "lower": 1.0, "upper": 4.0},
                "work_mem": {
                    "type": "categorical",
                    "values": [1024, 2048, 4096, 8192, 16384, 32768],
                },
                "effective_io_concurrency": {"type": "integer", "lower": 0, "upper": 200},
            },
        },
        "method_parameters": {
            "initial_observations": 4,
            "candidate_pool_size": 2048,
            "posterior_samples": 256,
            "max_attempts": 3,
        },
        "resource_limits": {
            "target_cpus": 4,
            "target_memory_bytes": 4_294_967_296,
            "docker_memory_bytes": 8_282_886_144,
        },
        "cache_policy": "blocked-randomized-no-manual-cache-drop",
        "software_versions": {"charmdb": "0.1.0", "postgresql": "18.1"},
        "budget_accounting": accounting,
        "objective_definition": {
            "maximize": ["throughput_tps", "negative_p99_ms"],
            "reference_point": [0.0, -20.0],
        },
        "constraint_definition": {"p99_ms_max": 20.0, "failures_max": 0},
        "practical_margins": {
            "throughput_relative": 0.05,
            "p99_ms_absolute": 1.0,
            "hypervolume_relative": 0.05,
        },
    }


def test_manifest_validation_and_hash_are_deterministic() -> None:
    definition = execution_definition()
    validate_execution_definition(definition, 20)
    first = canonical_manifest_sha256({"b": 2, "a": definition})
    repeated = canonical_manifest_sha256({"a": definition, "b": 2})

    assert first == repeated


def test_manifest_rejects_secrets_and_invalid_drift_budget() -> None:
    secret = execution_definition()
    secret["resource_limits"]["database_password"] = "must-not-persist"
    with pytest.raises(ValueError, match="secret-like"):
        validate_execution_definition(secret, 20)

    drift = execution_definition("DRIFT_PHASE_UNITS")
    with pytest.raises(ValueError, match="sum"):
        validate_execution_definition(drift, 20)

    placeholder = execution_definition()
    placeholder["cache_policy"] = "REPLACE_WITH_POLICY"
    with pytest.raises(ValueError, match="placeholder"):
        validate_execution_definition(placeholder, 20)


def test_prerequisite_decisions_fail_closed() -> None:
    measurement = decide_prerequisite_gate(
        "measurement_validity",
        {
            "measured_f3_trials": 5,
            "active_configuration_matched_f3_trials": 5,
            "artifact_backed_f3_trials": 5,
        },
    )
    fidelity = decide_prerequisite_gate(
        "paired_fidelity_gate",
        {"paired_candidates": 4, "early_stopping_enabled": False},
    )
    unknown = decide_prerequisite_gate("future_gate", {})

    assert measurement.eligible
    assert not fidelity.eligible
    assert not unknown.eligible
    assert "unknown prerequisite" in unknown.reasons[0]


def test_measurement_gate_includes_safely_rolled_back_f3_outcomes() -> None:
    assert set(MEASURED_F3_TERMINAL_STATES) == {
        "COMPLETED",
        "SLO_VIOLATED",
        "ROLLED_BACK",
    }


def test_f3_equivalent_accounting_preserves_full_and_failed_cost() -> None:
    objectives = {"throughput_tps": 300.0, "p99_ms": 40.0}

    assert f3_equivalent_unit_value(590.0, 600.0, 3, objectives) == 1.0
    assert f3_equivalent_unit_value(660.0, 600.0, 3, objectives) == 1.1
    assert f3_equivalent_unit_value(120.0, 600.0, 3, {}) == 0.2
    assert f3_equivalent_unit_value(300.0, 600.0, 2, objectives) == 0.5
