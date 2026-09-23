from __future__ import annotations

import json
import uuid
from copy import deepcopy
from pathlib import Path

import pytest

from charmdb.campaigns.f4 import F4_MANIFEST, _proposal_sha256, build_f4_plan
from charmdb.campaigns.h25 import (
    H25_MANIFEST,
    _differing_settings,
    analyze_h25_observations,
    build_h25_plan,
    h25_manifest_payload,
)
from charmdb.protocol import load_manifest

F4_SEEDS = {1: 1076286005, 2: 1762147992, 3: 398860006, 4: 2004491857}
CHAMPION_SOURCE = uuid.UUID("e6d70fab-0c90-5795-96a1-5021bff1194c")
OUTCOMES = {"DEFAULT": (3000.0, 28.0), "H25": (3090.0, 26.5), "E": (3150.0, 25.0)}


def _rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for slot in build_h25_plan():
        tps, p99 = OUTCOMES[slot.treatment]
        rows.append(
            {
                "physical_position": slot.physical_position,
                "repetition_block": slot.repetition_block,
                "within_block_position": slot.within_block_position,
                "treatment": slot.treatment,
                "source_primary_run_id": slot.source_primary_run_id,
                "random_seed": slot.random_seed,
                "requested_configuration": slot.configuration,
                "status": "COMPLETED",
                "infrastructure_attempts": 1,
                "authenticated": True,
                "valid": True,
                "objective_values": {"throughput_tps": tps, "p99_ms": p99},
                "lifecycle_seconds": 1652.0,
            }
        )
    return rows


def _f4_analysis() -> dict[str, object]:
    def summary(mean: float) -> dict[str, float]:
        return {"mean": mean}

    observations = []
    for block, seed in F4_SEEDS.items():
        observations.append(
            {
                "repetition_block": block,
                "treatment": "DEFAULT",
                "random_seed": seed,
                "throughput_tps": 2900.0,
                "p99_ms": 29.0,
            }
        )
        observations.append(
            {
                "repetition_block": block,
                "treatment": "E",
                "random_seed": seed,
                "throughput_tps": 3050.0,
                "p99_ms": 26.0,
            }
        )
    return {
        "decision": "CONFIRMED_CHAMPION",
        "selected_treatment": "E",
        "selected_primary_run_id": str(CHAMPION_SOURCE),
        "candidate_summaries": [
            {
                "treatment": label,
                "control_relative_tps_percent": summary(tps),
                "control_relative_p99_ms": summary(p99),
            }
            for label, tps, p99 in (
                ("T", 5.5, -3.1),
                ("L", 4.6, -2.6),
                ("H", 5.3, -2.5),
                ("E", 5.0, -3.0),
            )
        ],
        "observations": observations,
    }


def test_h25_plan_has_twelve_slots_with_default_at_center_on_the_f4_seeds() -> None:
    plan = build_h25_plan()
    f4_plan = build_f4_plan()
    default = next(slot.configuration for slot in f4_plan if slot.treatment == "DEFAULT")
    champion = next(slot for slot in f4_plan if slot.treatment == "E")

    assert len(plan) == 12
    assert [slot.treatment for slot in plan] == [
        "H25",
        "DEFAULT",
        "E",
        "E",
        "DEFAULT",
        "H25",
        "E",
        "DEFAULT",
        "H25",
        "H25",
        "DEFAULT",
        "E",
    ]
    assert len({_proposal_sha256(slot) for slot in plan}) == 12
    for block, seed in F4_SEEDS.items():
        rows = [slot for slot in plan if slot.repetition_block == block]
        assert [slot.within_block_position for slot in rows] == [1, 2, 3]
        assert rows[1].treatment == "DEFAULT"
        assert {slot.random_seed for slot in rows} == {seed}
    for slot in plan:
        if slot.treatment == "DEFAULT":
            assert slot.configuration == default
            assert slot.source_primary_run_id is None
        elif slot.treatment == "H25":
            assert slot.configuration == {**default, "shared_buffers": "131072"}
            assert slot.source_primary_run_id is None
        else:
            assert slot.configuration == champion.configuration
            assert slot.source_primary_run_id == CHAMPION_SOURCE
    for label in ("H25", "E"):
        positions = sorted(slot.within_block_position for slot in plan if slot.treatment == label)
        assert positions == [1, 1, 3, 3]


def test_h25_analysis_reports_contemporaneous_share_of_champion_gain() -> None:
    _, payload = h25_manifest_payload()

    analysis = analyze_h25_observations(_rows(), [], payload, _f4_analysis())

    assert analysis["decision"] == "H25_MEASURED"
    assert analysis["all_twelve_observations_valid"] is True
    assert analysis["counts"] == {
        "physical_slots": 12,
        "valid_slots": 12,
        "candidate_failures": 0,
        "retained_infrastructure_failures": 0,
    }
    assert len(analysis["block_contrasts"]) == 4
    for contrast in analysis["block_contrasts"]:
        assert contrast["h25_relative_tps_percent"] == pytest.approx(3.0)
        assert contrast["champion_relative_tps_percent"] == pytest.approx(5.0)
        assert contrast["h25_p99_delta_ms"] == pytest.approx(-1.5)
        assert contrast["champion_p99_delta_ms"] == pytest.approx(-3.0)
        assert contrast["share_of_champion_tps_gain"] == pytest.approx(0.6)
        assert contrast["share_of_champion_p99_gain"] == pytest.approx(0.5)
        assert contrast["h25_vs_champion_tps_percent"] == pytest.approx(3090.0 / 3150.0 * 100 - 100)
        assert contrast["h25_vs_champion_p99_delta_ms"] == pytest.approx(1.5)
        assert contrast["order"][1] == "DEFAULT"
    summaries = analysis["summaries"]
    assert summaries["h25_control_relative_tps_percent"]["mean"] == pytest.approx(3.0)
    assert summaries["champion_control_relative_tps_percent"]["mean"] == pytest.approx(5.0)
    assert summaries["share_of_champion_tps_gain"]["mean"] == pytest.approx(0.6)
    assert summaries["share_of_champion_p99_gain"]["n"] == 4
    context = analysis["champion_context"]
    assert context["champion_treatment"] == "E"
    assert context["source_primary_run_id"] == str(CHAMPION_SOURCE)
    contemporaneous = context["contemporaneous"]
    assert contemporaneous["share_of_champion_tps_gain_ratio_of_means"] == pytest.approx(0.6)
    assert contemporaneous["share_of_champion_p99_gain_ratio_of_means"] == pytest.approx(0.5)
    reference = context["f4_reference"]
    assert reference["contemporaneous"] is False
    assert reference["champion_f4_mean_control_relative_tps_percent"] == pytest.approx(5.0)
    assert reference["champion_effect_shift_tps_percentage_points"] == pytest.approx(0.0)
    assert set(reference["finalist_control_relative_means"]) == {"T", "L", "H", "E"}
    assert len(analysis["same_seed_context"]) == 4
    for row in analysis["same_seed_context"]:
        assert row["contemporaneous"] is False
        assert row["default_drift_percent_vs_f4"] == pytest.approx(3000.0 / 2900.0 * 100 - 100)
        assert row["champion_drift_percent_vs_f4"] == pytest.approx(3150.0 / 3050.0 * 100 - 100)
    assert analysis["treatment_configurations"]["H25"]["shared_buffers"] == "131072"
    assert analysis["treatment_configurations"]["E"]["shared_buffers"] == "182848"
    assert analysis["inference_limits"]["formal_superiority_claim_authorized"] is False
    assert analysis["inference_limits"]["champion_selection_unchanged"] is True


def test_h25_invalid_observation_yields_incomplete_decision() -> None:
    _, payload = h25_manifest_payload()
    rows = deepcopy(_rows())
    rows[0]["valid"] = False
    rows[0]["status"] = "CANDIDATE_FAILED"

    analysis = analyze_h25_observations(rows, [], payload, _f4_analysis())

    assert analysis["decision"] == "H25_INCOMPLETE"
    assert analysis["summaries"] == {}
    assert analysis["block_contrasts"] == []
    assert (
        analysis["champion_context"]["contemporaneous"]["share_of_champion_tps_gain_ratio_of_means"]
        is None
    )
    assert analysis["counts"]["valid_slots"] == 11
    assert analysis["counts"]["candidate_failures"] == 1


def test_h25_analysis_rejects_incomplete_or_unbalanced_slot_sets() -> None:
    _, payload = h25_manifest_payload()
    rows = _rows()

    with pytest.raises(ValueError, match="12 authenticated"):
        analyze_h25_observations(rows[:11], [], payload, _f4_analysis())
    unbalanced = deepcopy(rows)
    unbalanced[0]["treatment"] = "E"
    with pytest.raises(ValueError, match="complete H25/DEFAULT/E blocks"):
        analyze_h25_observations(unbalanced, [], payload, _f4_analysis())
    off_center = deepcopy(rows)
    off_center[0]["within_block_position"] = 2
    off_center[1]["within_block_position"] = 1
    with pytest.raises(ValueError, match="complete H25/DEFAULT/E blocks"):
        analyze_h25_observations(off_center, [], payload, _f4_analysis())


def test_h25_manifest_payload_rejects_tampered_or_foreign_manifests(tmp_path: Path) -> None:
    manifest = load_manifest(H25_MANIFEST)
    tampered = deepcopy(manifest.payload)
    tampered["treatments"]["H25"]["requested_configuration"]["work_mem"] = "8192"
    path = tmp_path / "h25-tampered.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    reordered = deepcopy(manifest.payload)
    reordered["design"]["schedule"][0]["order"] = ["DEFAULT", "H25", "E"]
    reordered_path = tmp_path / "h25-reordered.json"
    reordered_path.write_text(json.dumps(reordered), encoding="utf-8")
    champion_swapped = deepcopy(manifest.payload)
    champion_swapped["treatments"]["E"]["requested_configuration"]["work_mem"] = "1024"
    champion_path = tmp_path / "h25-champion.json"
    champion_path.write_text(json.dumps(champion_swapped), encoding="utf-8")

    with pytest.raises(ValueError, match="only shared_buffers"):
        h25_manifest_payload(path)
    with pytest.raises(ValueError, match="counterbalanced"):
        h25_manifest_payload(reordered_path)
    with pytest.raises(ValueError, match="frozen F4 finalist E"):
        build_h25_plan(champion_path)
    with pytest.raises(ValueError, match="wrong stage"):
        h25_manifest_payload(F4_MANIFEST)


def test_h25_canonical_settings_gate_names_the_knobs_an_active_champion_changes() -> None:
    expected = {
        "shared_buffers": "16384",
        "work_mem": "4096",
        "random_page_cost": "4",
        "effective_io_concurrency": "16",
    }
    champion_live = {
        "shared_buffers": "182848",
        "work_mem": "18022",
        "random_page_cost": "3.65532",
        "effective_io_concurrency": "16",
    }

    assert _differing_settings(expected, dict(expected)) == []
    assert _differing_settings(expected, champion_live) == [
        "random_page_cost",
        "shared_buffers",
        "work_mem",
    ]
    assert _differing_settings(expected, {}) == sorted(expected)
