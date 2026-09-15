from __future__ import annotations

import uuid
from pathlib import Path

from charmdb.campaigns.saturation import (
    PHASE1_ANCHOR_ORDER,
    PHASE1_ANCHORS,
    PHASE1_CONCURRENCY,
    PHASE1_SCALES,
    SaturationStep,
    _manifest_payload,
    detect_saturation_knee,
    saturation_step_dict,
)

ROOT = Path(__file__).resolve().parents[3]


def test_ready_manifest_exactly_matches_fixed_phase1_design() -> None:
    digest, payload = _manifest_payload(
        ROOT / "experiments/thesis/manifests/sanity-and-saturation.json"
    )

    assert len(digest) == 64
    assert payload["authorization_scope"] == "phase_1_and_phase_2"
    assert tuple(payload["phase_1"]["scales"]) == PHASE1_SCALES
    assert tuple(payload["phase_1"]["concurrency"]) == PHASE1_CONCURRENCY
    assert tuple(payload["phase_1"]["anchor_configurations"]) == PHASE1_ANCHOR_ORDER
    assert payload["phase_1"]["anchor_values"] == PHASE1_ANCHORS

    phase2 = payload["phase_2"]
    assert phase2["status"] == "passed"
    assert phase2["pgbench_maintenance_policy"] == (
        "canonical-baseline-only-no-vacuum"
    )
    assert phase2["pgbench_maintenance_arguments"] == ["--no-vacuum"]
    assert phase2["representative_configuration_count"] == 8
    assert len(phase2["representative_configurations"]) == 8


def test_phase1_plan_contains_exactly_45_probes() -> None:
    assert len(PHASE1_SCALES) * len(PHASE1_ANCHOR_ORDER) * len(PHASE1_CONCURRENCY) == 45


def test_knee_requires_flat_tps_and_material_p99_growth() -> None:
    points = [
        {"concurrency": 4, "throughput_tps": 700.0, "p99_ms": 7.0},
        {"concurrency": 8, "throughput_tps": 1300.0, "p99_ms": 9.0},
        {"concurrency": 16, "throughput_tps": 2200.0, "p99_ms": 12.0},
        {"concurrency": 32, "throughput_tps": 3000.0, "p99_ms": 28.0},
        {"concurrency": 64, "throughput_tps": 3001.0, "p99_ms": 57.0},
    ]

    assert detect_saturation_knee(points) == 32


def test_saturation_step_json_preserves_lineage() -> None:
    ids = [uuid.uuid4() for _ in range(4)]
    payload = saturation_step_dict(SaturationStep("probe-executed", *ids, {"state": "COMPLETED"}))

    assert payload["campaign_id"] == str(ids[0])
    assert payload["trial_id"] == str(ids[3])
    assert payload["details"] == {"state": "COMPLETED"}
