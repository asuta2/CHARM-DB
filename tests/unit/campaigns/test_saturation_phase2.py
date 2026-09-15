from __future__ import annotations

import uuid
from pathlib import Path

from charmdb.campaigns.saturation_phase2 import (
    PHASE2_CONFIGURATION_COUNT,
    Phase2Step,
    nondominated_phase2_points,
    phase2_manifest_payload,
    phase2_step_dict,
)

ROOT = Path(__file__).resolve().parents[3]


def test_phase2_manifest_freezes_accepted_eight_configuration_design() -> None:
    digest, payload = phase2_manifest_payload(
        ROOT / "experiments/thesis/manifests/sanity-and-saturation.json"
    )
    phase = payload["phase_2"]

    assert len(digest) == 64
    assert phase["restore_overhead_accepted"] is True
    assert len(phase["representative_configurations"]) == PHASE2_CONFIGURATION_COUNT
    assert len({item["name"] for item in phase["representative_configurations"]}) == 8
    assert all(len(item["values"]) == 11 for item in phase["representative_configurations"])


def test_phase2_pareto_analysis_maximizes_tps_and_minimizes_p99() -> None:
    points = [
        {"configuration_name": "fast", "throughput_tps": 3100.0, "p99_ms": 55.0},
        {"configuration_name": "balanced", "throughput_tps": 3000.0, "p99_ms": 35.0},
        {"configuration_name": "low-latency", "throughput_tps": 2800.0, "p99_ms": 25.0},
        {"configuration_name": "dominated", "throughput_tps": 2700.0, "p99_ms": 60.0},
    ]

    names = {
        point["configuration_name"] for point in nondominated_phase2_points(points)
    }
    assert names == {"fast", "balanced", "low-latency"}


def test_phase2_step_json_preserves_lineage() -> None:
    campaign_id, configuration_id, trial_id = (uuid.uuid4() for _ in range(3))
    payload = phase2_step_dict(
        Phase2Step(
            "configuration-executed",
            campaign_id,
            configuration_id,
            trial_id,
            {"trial_state": "COMPLETED"},
        )
    )

    assert payload["campaign_id"] == str(campaign_id)
    assert payload["configuration_id"] == str(configuration_id)
    assert payload["trial_id"] == str(trial_id)
