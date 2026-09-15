from __future__ import annotations

import copy
import json
import math
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from charmdb.campaigns.screening import (
    PARAMETER_ORDER,
    ScreeningStep,
    _oat_configuration,
    analyze_screening_observations,
    decode_screening_point,
    export_screening_design,
    partial_rank_correlations,
    screening_design_summary,
    screening_initial_schedule,
    screening_manifest_payload,
    screening_oat_plan_rows,
    screening_plan_rows,
    screening_sobol_configurations,
    screening_step_dict,
)

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "experiments/thesis/manifests/parameter-screening.json"


def _analysis_points(payload: dict[str, object]) -> list[dict[str, object]]:
    points: list[dict[str, object]] = []
    for item in screening_initial_schedule(payload):
        configuration = dict(item["configuration"])
        default = item["kind"] == "DEFAULT_CONTROL"
        points.append(
            {
                "phase": "INITIAL",
                "evaluation_kind": item["kind"],
                "chronological_execution_index": item["position"],
                "requested_configuration": configuration,
                "status": "COMPLETED",
                "throughput_tps": (
                    2700.0 if default else 2000.0 + float(configuration["shared_buffers"]) / 100
                ),
                "p99_ms": 30.0,
                "failures": 0,
                "exact_core_passed": True,
                "physical_statistics_passed": True,
            }
        )
    return points


def test_screening_manifest_freezes_fresh_seed_design_and_runtime() -> None:
    digest, payload = screening_manifest_payload(MANIFEST)
    summary = screening_design_summary(MANIFEST)

    assert len(digest) == 64
    assert payload["status"] == "ready"
    assert payload["execution_ready"] is True
    assert payload["design"]["screening_seed"] == 1783446633
    assert payload["runtime"]["accepted"] is True
    assert summary["initial_observations"] == 35
    assert summary["maximum_observations"] == 41


def test_sobol_design_and_interleaved_schedule_are_exact_and_unique() -> None:
    _, payload = screening_manifest_payload(MANIFEST)
    configurations = screening_sobol_configurations(payload)
    schedule = screening_initial_schedule(payload)

    assert len(configurations) == 32
    assert len({json.dumps(item["configuration"], sort_keys=True) for item in configurations}) == 32
    assert len(schedule) == 35
    assert [item["position"] for item in schedule if item["kind"] == "DEFAULT_CONTROL"] == [
        1,
        18,
        35,
    ]
    assert sum(item["kind"] == "SOBOL" for item in schedule) == 32


def test_durable_plan_rows_are_complete_bounded_and_deterministic() -> None:
    _, payload = screening_manifest_payload(MANIFEST)
    block_id = uuid.uuid4()
    campaign_id = uuid.uuid4()

    initial = screening_plan_rows(payload, block_id, campaign_id)
    repeated = screening_plan_rows(payload, block_id, campaign_id)
    oat = screening_oat_plan_rows(
        payload,
        block_id,
        campaign_id,
        ["work_mem", "max_wal_size", "random_page_cost"],
    )

    assert initial == repeated
    assert len(initial) == 35
    assert [row["sequence"] for row in initial] == list(range(1, 36))
    assert len({row["run_id"] for row in initial}) == 35
    assert len(oat) == 6
    assert [row["sequence"] for row in oat] == list(range(36, 42))
    assert all(row["phase"] == "OAT" for row in oat)

    with pytest.raises(ValueError, match="at most three"):
        screening_oat_plan_rows(
            payload,
            block_id,
            campaign_id,
            ["work_mem", "max_wal_size", "random_page_cost", "shared_buffers"],
        )


def test_dimension_independent_decoder_respects_every_bound() -> None:
    _, payload = screening_manifest_payload(MANIFEST)
    low = decode_screening_point([0.0] * len(PARAMETER_ORDER), payload)
    almost_high = decode_screening_point([1.0 - 1e-12] * len(PARAMETER_ORDER), payload)

    parameters = payload["search_space"]["parameters"]
    assert list(low) == list(PARAMETER_ORDER)
    for name in PARAMETER_ORDER:
        assert Decimal(low[name]) == Decimal(str(parameters[name]["lower"]))
        assert Decimal(almost_high[name]) <= Decimal(str(parameters[name]["upper"]))


def test_frozen_design_hash_rejects_post_registration_changes(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    changed = copy.deepcopy(payload)
    changed["design"]["screening_seed"] += 1
    path = tmp_path / "parameter-screening.json"
    path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(ValueError, match="fresh reproducible seed"):
        screening_manifest_payload(path)


def test_design_export_is_complete_and_confined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "parameter-screening.json"
    manifest.write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = export_screening_design(manifest)
    output = Path(result["output_path"])
    exported = json.loads(output.read_text(encoding="utf-8"))

    assert len(exported["schedule"]) == 35
    assert exported["sobol_design_sha256"] == (
        "b533c8eb540f8022e5878784c2b5a224d56d65e5688eb86ae856232cb93757cb"
    )
    with pytest.raises(ValueError, match="must stay under"):
        export_screening_design(manifest, tmp_path / "outside.json")


def test_prcc_identifies_a_monotonic_conditioned_effect() -> None:
    _, payload = screening_manifest_payload(MANIFEST)
    configurations = [
        dict(item["configuration"]) for item in screening_sobol_configurations(payload)
    ]
    response = [float(configuration["shared_buffers"]) for configuration in configurations]

    correlations = partial_rank_correlations(configurations, response)

    assert correlations["shared_buffers"] == pytest.approx(1.0)
    assert set(correlations) == set(PARAMETER_ORDER)


def test_initial_analysis_passes_or_plans_only_bounded_oat() -> None:
    _, payload = screening_manifest_payload(MANIFEST)

    analysis = analyze_screening_observations(_analysis_points(payload), payload)

    assert analysis["valid_sobol_measurements"] == 32
    assert analysis["control_drift"]["passed"] is True
    assert analysis["outcome"] in {"PASSED", "OAT_REQUIRED"}
    assert len(analysis["oat_parameters"]) <= 3


def test_analysis_blocks_drift_and_insufficient_validity() -> None:
    _, payload = screening_manifest_payload(MANIFEST)
    drifting = _analysis_points(payload)
    controls = [point for point in drifting if point["evaluation_kind"] == "DEFAULT_CONTROL"]
    for point, tps in zip(controls, (2000.0, 2500.0, 3000.0), strict=True):
        point["throughput_tps"] = tps
    drift_analysis = analyze_screening_observations(drifting, payload)
    assert drift_analysis["outcome"] == "BLOCKED_DRIFT"

    invalid = _analysis_points(payload)
    sobol = [point for point in invalid if point["evaluation_kind"] == "SOBOL"]
    for point in sobol[:5]:
        point["status"] = "FAILED"
    validity_analysis = analyze_screening_observations(invalid, payload)
    assert validity_analysis["valid_sobol_measurements"] == 27
    assert validity_analysis["outcome"] == "BLOCKED_VALIDITY"


def test_oat_endpoints_change_only_the_target_knob() -> None:
    _, payload = screening_manifest_payload(MANIFEST)
    low = _oat_configuration(payload, "work_mem", "lower")
    high = _oat_configuration(payload, "work_mem", "upper")

    assert low["work_mem"] == "1024"
    assert high["work_mem"] == "32768"
    assert {key: value for key, value in low.items() if key != "work_mem"} == {
        key: value for key, value in high.items() if key != "work_mem"
    }


def test_final_analysis_resolves_only_the_frozen_oat_pair() -> None:
    _, payload = screening_manifest_payload(MANIFEST)
    points = _analysis_points(payload)
    sobol = [point for point in points if point["evaluation_kind"] == "SOBOL"]
    shared = [float(dict(point["requested_configuration"])["shared_buffers"]) for point in sobol]
    lower = min(shared)
    span = max(shared) - lower
    for index, (point, value) in enumerate(zip(sobol, shared, strict=True)):
        point["throughput_tps"] = 2500 + 100 * (
            (value - lower) / span + 0.2 * math.sin(index * 2.1)
        )
    initial = analyze_screening_observations(points, payload)
    assert initial["outcome"] == "OAT_REQUIRED"
    assert initial["oat_parameters"] == ["max_wal_size"]

    for offset, (kind, endpoint, throughput) in enumerate(
        (("OAT_LOW", "lower", 2500.0), ("OAT_HIGH", "upper", 2700.0))
    ):
        points.append(
            {
                "phase": "OAT",
                "evaluation_kind": kind,
                "oat_parameter": "max_wal_size",
                "chronological_execution_index": 36 + offset,
                "requested_configuration": _oat_configuration(
                    payload, "max_wal_size", endpoint
                ),
                "status": "COMPLETED",
                "throughput_tps": throughput,
                "p99_ms": 30.0,
                "failures": 0,
                "exact_core_passed": True,
                "physical_statistics_passed": True,
            }
        )

    final = analyze_screening_observations(points, payload, initial_analysis=initial)

    assert final["outcome"] == "PASSED"
    assert final["oat_results"]["max_wal_size"]["included"] is True
    assert "max_wal_size" in final["selection"]["included"]


def test_step_json_preserves_screening_lineage() -> None:
    step = ScreeningStep(
        "run-executed",
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        {"phase": "INITIAL"},
    )

    payload = screening_step_dict(step)

    assert payload["action"] == "run-executed"
    assert payload["details"]["phase"] == "INITIAL"
    assert payload["run_id"] is not None
