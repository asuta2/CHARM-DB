from __future__ import annotations

import json
from pathlib import Path

import pytest

from charmdb.analysis.rehearsal import (
    REHEARSAL_SCENARIOS,
    SYNTHETIC_NOTICE,
    rehearsal_invariants,
    run_analysis_rehearsal,
    synthetic_wave_a_rows,
)
from charmdb.campaigns.primary import analyze_primary_observations
from charmdb.optimization.design import PRIMARY_MANIFEST, build_wave_a_schedule
from charmdb.protocol import load_manifest

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / PRIMARY_MANIFEST
PAYLOAD = load_manifest(MANIFEST).payload


def test_synthetic_ledger_reproduces_the_frozen_schedule_identity() -> None:
    schedule = build_wave_a_schedule(MANIFEST)
    for scenario in REHEARSAL_SCENARIOS:
        rows = synthetic_wave_a_rows(scenario, MANIFEST)
        assert len(rows) == 393
        for row, entry in zip(rows, schedule, strict=True):
            assert row["global_position"] == entry.global_position
            assert row["seed"] == entry.seed
            assert row["within_seed_position"] == entry.within_seed_position
            assert row["method"] == entry.method
            assert row["budget_position"] == entry.budget_position
            assert row["evaluation_role"] == entry.evaluation_role


def test_unsupported_scenario_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported rehearsal scenario"):
        synthetic_wave_a_rows("optimistic", MANIFEST)


@pytest.mark.parametrize("scenario", REHEARSAL_SCENARIOS)
def test_each_scenario_satisfies_every_structural_invariant(scenario: str) -> None:
    analysis = analyze_primary_observations(synthetic_wave_a_rows(scenario, MANIFEST), PAYLOAD)
    assert rehearsal_invariants(analysis, PAYLOAD) == []


def test_failed_and_retried_slots_never_become_valid_observations() -> None:
    rows = synthetic_wave_a_rows("failures-and-retries", MANIFEST)
    analysis = analyze_primary_observations(rows, PAYLOAD)

    assert analysis["terminal_counts"]["CANDIDATE_FAILED"] > 0
    assert analysis["terminal_counts"]["INFRASTRUCTURE_EXHAUSTED"] > 0
    assert analysis["physical_valid_candidates"] < 378
    retained = sum(
        int(item["retained_infrastructure_attempts"]) for item in analysis["failure_accounting"]
    )
    assert retained > 0
    scattered = {str(item["global_position"]) for item in analysis["candidate_scatter"]}
    for row in rows:
        if row["status"] != "COMPLETED" or (row["constraint_values"] or {}).get("failures"):
            assert str(row["global_position"]) not in scattered


def test_drift_scenario_flags_every_seed_without_killing_the_campaign() -> None:
    analysis = analyze_primary_observations(
        synthetic_wave_a_rows("drift-flagged", MANIFEST), PAYLOAD
    )
    assert analysis["outcome"] == "COMPLETE_WITH_DRIFT_FLAGS"
    assert all(item["flagged"] is True for item in analysis["drift_flags"])
    assert all(item["campaign_killing"] is False for item in analysis["drift_flags"])
    assert analysis["physical_valid_candidates"] == 378


def test_rehearsal_writes_a_labelled_synthetic_package(tmp_path: Path) -> None:
    summary = run_analysis_rehearsal(tmp_path, ("nominal",), MANIFEST)

    assert summary["passed"] is True
    assert summary["synthetic"] is True
    assert summary["evidence_role"] == "INFRASTRUCTURE"
    assert summary["creates_primary_campaign"] is False
    assert summary["touches_target_database"] is False
    assert summary["schedule_sha256"] == PAYLOAD["execution_schedule"]["schedule_sha256"]
    assert SYNTHETIC_NOTICE in (tmp_path / "README-SYNTHETIC.md").read_text(encoding="utf-8")
    assert not (tmp_path / ".determinism-check").exists()

    scenario = summary["scenarios"][0]
    assert scenario["violations"] == []
    assert scenario["deterministic_rerender"] is True
    index = json.loads(
        (tmp_path / "nominal" / "primary-report-index.json").read_text(encoding="utf-8")
    )
    assert index["context"]["synthetic"] is True
    assert index["context"]["evidence_role"] == "INFRASTRUCTURE"
    assert index["payload_sha256"] == scenario["report_payload_sha256"]

    persisted = json.loads((tmp_path / "rehearsal-summary.json").read_text(encoding="utf-8"))
    assert persisted["result_sha256"] == summary["result_sha256"]


def test_invariants_report_a_truncated_trajectory() -> None:
    analysis = analyze_primary_observations(synthetic_wave_a_rows("nominal", MANIFEST), PAYLOAD)
    analysis["slot_trajectories"][0]["slots"] = analysis["slot_trajectories"][0]["slots"][:-1]
    violations = rehearsal_invariants(analysis, PAYLOAD)
    assert any("contiguous 1-30 logical budget" in item for item in violations)
