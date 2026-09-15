from __future__ import annotations

import copy
import json
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from charmdb.campaigns.primary import _canonical_sha256, analyze_primary_observations
from charmdb.campaigns.primary_wave_b import (
    WAVE_B_SEEDS,
    _wave_b_analysis_payload,
    _wave_b_interruption_blockers,
    combine_primary_analyses,
    wave_b_design_summary,
)
from charmdb.optimization.design import (
    PRIMARY_MANIFEST,
    PRIMARY_WAVE_B_MANIFEST,
    build_wave_a_plan,
    build_wave_b_plan,
    primary_candidate_design_sha256_for_seeds,
    schedule_sha256,
)
from charmdb.protocol import PRIMARY_SEARCH_METHODS, load_manifest, validate_manifest
from charmdb.reporting.primary import FIGURES, primary_tables, render_primary_report

ROOT = Path(__file__).resolve().parents[3]
PRIMARY_PATH = ROOT / PRIMARY_MANIFEST
WAVE_B_PATH = ROOT / PRIMARY_WAVE_B_MANIFEST


METHOD_GAIN = {
    "random": 0.01,
    "sobol": 0.02,
    "bo_shared_initial": 0.03,
    "bo_qlognei_throughput": 0.03,
    "bo_qlognparego_multiobjective": 0.04,
    "bo_qlognehvi_multiobjective": 0.05,
}


def _complete_rows(plan: tuple[object, ...], namespace: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for raw in plan:
        item = raw
        entry = item.schedule
        control_tps = 1000.0 + 0.1 * entry.within_seed_position
        control_p99 = 26.0 + 0.01 * entry.within_seed_position
        gain = METHOD_GAIN.get(entry.method, 0.0)
        is_control = entry.method == "postgresql_default"
        rows.append(
            {
                "primary_run_id": uuid.uuid5(
                    uuid.NAMESPACE_URL, f"{namespace}:{entry.global_position}"
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
                    "throughput_tps": control_tps if is_control else control_tps * (1 + gain),
                    "p99_ms": control_p99 if is_control else control_p99 - gain * 100,
                },
                "constraint_values": {"failures": 0},
            }
        )
    return rows


def test_wave_b_design_is_exact_and_result_blind() -> None:
    wave_payload = load_manifest(WAVE_B_PATH).payload
    primary_payload = load_manifest(PRIMARY_PATH).payload
    plan = build_wave_b_plan(wave_payload, primary_payload)
    summary = wave_b_design_summary(WAVE_B_PATH)

    assert tuple(wave_payload["wave_b"]["seeds"]) == WAVE_B_SEEDS
    assert len(plan) == 262
    assert summary["physical_observations"] == 262
    assert summary["method_physical_observations"] == {
        "bo_qlognehvi_multiobjective": 36,
        "bo_qlognei_throughput": 36,
        "bo_qlognparego_multiobjective": 36,
        "bo_shared_initial": 24,
        "postgresql_default": 10,
        "random": 60,
        "sobol": 60,
    }
    assert schedule_sha256(tuple(item.schedule for item in plan)) == (
        "4b71b1b992c210dfb29aace7e15c99151a65d985537f32121597e3205f5f8c6e"
    )
    assert (
        primary_candidate_design_sha256_for_seeds(primary_payload, list(WAVE_B_SEEDS))
        == "679505326616f2c4eb1fdeccd1081a0d22896fd82d0bf5452ac1ddf8cf931358"
    )


def test_wave_b_ready_state_is_fail_closed_until_implementation_is_recorded() -> None:
    payload = copy.deepcopy(load_manifest(WAVE_B_PATH).payload)
    payload["prerequisites"]["durable_wave_b_runner_implemented"] = False
    with pytest.raises(ValueError, match="complete durable implementation"):
        validate_manifest(payload)


def test_interruption_reconciliation_requires_exact_power_loss_signature() -> None:
    campaign = {"campaign_status": "PAUSED", "block_status": "RUNNING", "wave": "B"}
    attempt = {
        "run_status": "CREATED",
        "attempt_status": "CREATED",
        "trial_state": "RUNNING_FULL_EVALUATION",
        "trial_completed_at": None,
        "lease_expired": True,
        "attempt_count": 1,
        "max_attempts": 1,
        "action_status": "STARTED",
        "action_completed_at": None,
        "restore_status": "PASSED",
        "exact_core_passed": True,
        "physical_statistics_passed": True,
        "later_started_runs": 0,
        "attempt_number": 1,
    }
    target = {
        "postmaster_restarted_after_action": True,
        "application_matches_trial": True,
        "snapshot_matches_default": True,
        "active_matches_trial": True,
        "pending_restart": [],
        "active_charm_sessions": 0,
        "managed_indexes": 0,
    }
    artifacts = {
        "directory_exists": True,
        "completed_markers": [],
        "trial_artifact_exists": False,
        "partial_logs": [{"relative_path": "partial", "byte_size": 1, "sha256": "a" * 64}],
    }

    assert _wave_b_interruption_blockers(campaign, attempt, target, artifacts) == []

    unsafe_target = {**target, "active_charm_sessions": 1}
    completed_artifacts = {**artifacts, "completed_markers": ["measurement-attempt-1.json"]}
    blockers = _wave_b_interruption_blockers(
        {**campaign, "campaign_status": "RUNNING"},
        {**attempt, "lease_expired": False},
        unsafe_target,
        completed_artifacts,
    )
    assert "campaign-not-paused" in blockers
    assert "trial-lease-is-not-expired" in blockers
    assert "target-has-active-charm-sessions" in blockers
    assert "completed-measurement-marker-exists" in blockers


def test_wave_b_migration_enforces_single_immutable_wave_b_block() -> None:
    migration = (ROOT / "migrations/041_v2_primary_wave_b.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS wave" in migration
    assert "WHERE wave = 'B'" in migration
    assert "OLD.wave" in migration
    assert "NEW.wave" in migration


def test_standalone_wave_b_and_combined_five_seed_analysis(tmp_path: Path) -> None:
    primary_payload = load_manifest(PRIMARY_PATH).payload
    wave_payload = load_manifest(WAVE_B_PATH).payload
    wave_b_plan = build_wave_b_plan(wave_payload, primary_payload)
    wave_b = analyze_primary_observations(
        _complete_rows(wave_b_plan, "wave-b"),
        _wave_b_analysis_payload(primary_payload),
        seeds=list(WAVE_B_SEEDS),
        expected_observations=262,
        report_scope="Wave B",
    )
    wave_b["standalone_before_combined"] = True
    wave_b["analysis_sha256"] = "test-wave-b-analysis"

    assert wave_b["outcome"] == "COMPLETE"
    assert wave_b["independent_seed_count"] == 2
    assert wave_b["terminal_counts"] == {"COMPLETED": 262}
    assert all(row["seed_count"] == 2 for row in wave_b["method_results"])

    wave_a = analyze_primary_observations(
        _complete_rows(build_wave_a_plan(PRIMARY_PATH), "wave-a"), primary_payload
    )
    combined = combine_primary_analyses(wave_a, wave_b, wave_payload)

    assert combined["outcome"] == "COMPLETE"
    assert combined["independent_seed_count"] == 5
    assert len(combined["seed_method_results"]) == 25
    assert combined["terminal_counts"] == {"COMPLETED": 655}
    assert combined["physical_valid_candidates"] == 630
    assert combined["valid_default_controls"] == 25
    assert len(combined["slot_trajectories"]) == 25
    assert len(combined["control_series"]) == 25
    assert combined["unified_five_seed_cohort"] is True
    assert combined["report_scope"] == "final five-seed"
    assert {row["method"] for row in combined["method_results"]} == set(PRIMARY_SEARCH_METHODS)
    comparison = next(
        row
        for row in combined["pairwise_comparisons"]
        if row["metric"] == "mean_control_relative_tps"
        and row["baseline_method"] == "random"
        and row["treatment_method"] == "bo_qlognehvi_multiobjective"
    )
    assert comparison["permutation_p_value"] == pytest.approx(0.0625)
    assert combined["apply_best_authorized"] is False
    assert "do not rank qLogNParEGO" in combined["interpretation_guard"]

    for trajectory in combined["slot_trajectories"]:
        hypervolume = [float(slot["hypervolume"]) for slot in trajectory["slots"]]
        assert hypervolume == sorted(hypervolume)
        throughput = [
            float(slot["best_throughput_tps"])
            for slot in trajectory["slots"]
            if slot["best_throughput_tps"] is not None
        ]
        assert throughput == sorted(throughput)
        p99 = [
            float(slot["minimum_p99_ms"])
            for slot in trajectory["slots"]
            if slot["minimum_p99_ms"] is not None
        ]
        assert p99 == sorted(p99, reverse=True)

    combined["analysis_sha256"] = _canonical_sha256(combined)
    context = {
        "analysis_sha256": combined["analysis_sha256"],
        "benchmark_profile_id": primary_payload["benchmark_profile_id"],
        "evidence_role": "PRIMARY",
        "report_kind": "thesis-protocol-v2-primary-final-five-seed",
        "report_filename": "final-five-seed-report.md",
        "index_filename": "final-five-seed-report-index.json",
        "source_provenance": {
            "wave_a_analysis_sha256": "test-a",
            "wave_b_analysis_sha256": "test-b",
        },
    }
    first = render_primary_report(combined, context, tmp_path / "first")
    second = render_primary_report(combined, context, tmp_path / "second")
    first_index = json.loads(Path(first["index_path"]).read_text(encoding="utf-8"))
    second_index = json.loads(Path(second["index_path"]).read_text(encoding="utf-8"))

    tables = primary_tables(combined)
    assert len(tables) == 9
    assert all(tables.values())
    assert len(FIGURES) == 6
    assert first["file_count"] == 17
    assert first["payload_sha256"] == second["payload_sha256"]
    assert first_index["files"] == second_index["files"]
    assert first_index["context"]["analysis_sha256"] == combined["analysis_sha256"]
    assert first_index["table_row_counts"] == {
        "default-controls": 25,
        "default-drift": 5,
        "method-outcomes": 5,
        "pairwise-contrasts": 50,
        "pareto-front": len(combined["pareto_front"]),
        "safety-and-failures": 6,
        "seed-level-statistics": 25,
        "seed-method-outcomes": 25,
        "slot-trajectories": 750,
    }
    rendered = [
        path
        for path in (tmp_path / "first").rglob("*")
        if path.is_file() and path.suffix in {".md", ".csv", ".svg"}
    ]
    presentation = "\n".join(path.read_text(encoding="utf-8") for path in rendered).lower()
    assert "wave a" not in presentation
    assert "wave b" not in presentation
    for figure in (tmp_path / "first/figures").glob("*.svg"):
        ET.parse(figure)
    assert (
        "within-seed scheduled position (1-131)"
        in (tmp_path / "first/figures/figure-default-drift-by-position.svg")
        .read_text(encoding="utf-8")
        .lower()
    )
