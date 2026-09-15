from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from charmdb.campaigns.primary import analyze_primary_observations, primary_manifest_payload
from charmdb.optimization.design import PRIMARY_MANIFEST, build_wave_a_plan
from charmdb.protocol import PRIMARY_SEARCH_METHODS, load_manifest

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / PRIMARY_MANIFEST


def _complete_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in build_wave_a_plan(MANIFEST):
        entry = item.schedule
        control_tps = 1000.0 + 0.1 * entry.within_seed_position
        control_p99 = 20.0 + 0.01 * entry.within_seed_position
        is_control = entry.method == "postgresql_default"
        rows.append(
            {
                "primary_run_id": uuid.uuid5(
                    uuid.NAMESPACE_URL, f"test-primary:{entry.global_position}"
                ),
                "global_position": entry.global_position,
                "seed_index": entry.seed_index,
                "seed": entry.seed,
                "within_seed_position": entry.within_seed_position,
                "evaluation_role": entry.evaluation_role,
                "method": entry.method,
                "budget_position": entry.budget_position,
                "shared_with_methods": list(entry.shared_with_methods),
                "status": "COMPLETED",
                "infrastructure_attempts": 0,
                "candidate_vector": (
                    list(item.candidate.vector) if item.candidate is not None else [0.5] * 8
                ),
                "requested_configuration": (
                    item.candidate.configuration if item.candidate is not None else {}
                ),
                "objective_values": {
                    "throughput_tps": control_tps if is_control else control_tps * 1.1,
                    "p99_ms": control_p99 if is_control else control_p99 - 2.0,
                },
                "constraint_values": {"failures": 0},
            }
        )
    return rows


def test_primary_analysis_uses_interpolated_controls_and_expands_shared_bo_only() -> None:
    payload = load_manifest(MANIFEST).payload
    analysis = analyze_primary_observations(_complete_rows(), payload)

    assert analysis["outcome"] == "COMPLETE"
    assert analysis["physical_valid_candidates"] == 378
    assert analysis["valid_default_controls"] == 15
    assert all(item["flagged"] is False for item in analysis["drift_flags"])
    results = {item["method"]: item for item in analysis["method_results"]}
    assert set(results) == set(PRIMARY_SEARCH_METHODS)
    for result in results.values():
        assert result["valid_observations"] == 90
        assert result["mean_control_relative_tps"] == pytest.approx(0.1)
        assert result["mean_control_relative_p99_ms"] == pytest.approx(-2.0)
    assert analysis["terminal_counts"] == {"COMPLETED": 393}


def test_primary_analysis_rejects_partial_ledger() -> None:
    payload = load_manifest(MANIFEST).payload
    with pytest.raises(ValueError, match="complete 393-slot ledger"):
        analyze_primary_observations(_complete_rows()[:-1], payload)


def test_primary_manifest_freezes_retry_nonconsumption_and_drift_is_non_killing() -> None:
    _, payload = primary_manifest_payload(MANIFEST)
    retry = payload["infrastructure_retry_policy"]
    drift = payload["drift_interpretation"]
    environment = payload["pre_campaign_environment_gates"]

    assert retry["maximum_trials_per_slot"] == 3
    assert retry["candidate_reused_on_retry"] is True
    assert retry["new_proposal_on_retry"] is False
    assert retry["optimizer_training_on_retryable_failure"] is False
    assert retry["candidate_budget_consumed_by_retry"] is False
    assert drift["flag_is_campaign_killing"] is False
    assert drift["global_default_constant_for_primary_contrasts"] is False
    assert environment["minimum_consecutive_restore_passes"] == 15
    assert environment["minimum_artifact_free_bytes"] == 50 * 1024**3
    assert environment["artifact_runtime_directory_must_be_outside_onedrive"] is True


def test_primary_migration_has_attempt_and_training_lineage_guards() -> None:
    migration = (ROOT / "migrations/037_v2_primary_comparison.sql").read_text(encoding="utf-8")

    assert "experiment_v2_primary_blocks" in migration
    assert "experiment_v2_primary_runs" in migration
    assert "experiment_v2_primary_attempts" in migration
    assert "experiment_v2_primary_training_lineage" in migration
    assert "invalid or cross-method v2 primary training lineage" in migration
    assert "materialized v2 primary proposal is immutable" in migration

    reliability = (ROOT / "migrations/038_v2_primary_restore_reliability.sql").read_text(
        encoding="utf-8"
    )
    assert "required_repetitions BETWEEN 15 AND 20" in reliability
    assert "terminal v2 primary restore soak is immutable" in reliability
    assert "terminal v2 primary restore-soak run is immutable" in reliability
