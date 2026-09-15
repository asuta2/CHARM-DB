from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from charmdb.campaigns import default_reference
from charmdb.campaigns.default_reference import (
    DefaultReferenceStep,
    analyze_reference_points,
    default_reference_manifest_payload,
    default_reference_step_dict,
)
from charmdb.config import Settings

ROOT = Path(__file__).resolve().parents[3]


def _criteria() -> dict[str, object]:
    _, payload = default_reference_manifest_payload(
        ROOT / "experiments/thesis/manifests/new-machine-default-reference.json"
    )
    return dict(payload["acceptance_criteria"])


def _points(tps: list[float], p99: list[float]) -> list[dict[str, object]]:
    return [
        {
            "chronological_execution_index": index,
            "throughput_tps": throughput,
            "p99_ms": latency,
            "failures": 0,
            "exact_core_passed": True,
            "physical_statistics_passed": True,
        }
        for index, (throughput, latency) in enumerate(zip(tps, p99, strict=True), start=1)
    ]


def test_manifest_preregisters_fresh_reproducible_seeds_and_fixed_contingency() -> None:
    digest, payload = default_reference_manifest_payload(
        ROOT / "experiments/thesis/manifests/new-machine-default-reference.json"
    )

    expected = [
        int.from_bytes(
            hashlib.sha256(
                f"thesis-protocol-v2/default-reference/primary/{index}".encode()
            ).digest()[:4],
            "big",
        )
        % 2_147_483_647
        for index in range(1, 6)
    ]
    assert len(digest) == 64
    assert payload["status"] == "ready"
    assert payload["primary_seeds"] == expected
    assert 20260731 not in payload["primary_seeds"]
    assert set(payload["primary_seeds"]).isdisjoint(payload["contingency_seeds"])
    assert payload["contingency"]["maximum_total_observations"] == 10
    assert payload["restore_inclusive_runtime_accepted"] is True


def test_five_precise_stable_valid_observations_pass() -> None:
    result = analyze_reference_points(
        _points(
            [100.0, 101.0, 99.0, 100.5, 99.5],
            [30.0, 30.5, 29.5, 30.2, 29.8],
        ),
        _criteria(),
    )

    assert result["validity_passed"] is True
    assert result["precision"]["passed"] is True
    assert result["drift"]["passed"] is True
    assert result["outcome"] == "PASSED"


def test_initial_imprecision_triggers_only_fixed_contingency() -> None:
    result = analyze_reference_points(
        _points([92.0, 108.0, 100.0, 108.0, 92.0], [30.0] * 5),
        _criteria(),
    )

    assert result["precision"]["tps_passed"] is False
    assert result["drift"]["passed"] is True
    assert result["outcome"] == "CONTINGENCY_REQUIRED"


def test_material_drift_blocks_without_averaging_it_away() -> None:
    result = analyze_reference_points(
        _points([100.0, 103.0, 106.0, 109.0, 112.0], [30.0] * 5),
        _criteria(),
    )

    assert result["drift"]["tps_passed"] is False
    assert result["outcome"] == "BLOCKED_DRIFT"


def test_ten_observations_cannot_extend_again() -> None:
    result = analyze_reference_points(
        _points(
            [80.0, 120.0, 120.0, 80.0, 100.0, 100.0, 80.0, 120.0, 120.0, 80.0],
            [30.0] * 10,
        ),
        _criteria(),
    )

    assert result["precision"]["tps_passed"] is False
    assert result["drift"]["passed"] is True
    assert result["outcome"] == "BLOCKED_IMPRECISION"


def test_step_json_preserves_default_control_lineage() -> None:
    step = DefaultReferenceStep(
        "run-executed",
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        {"chronological_execution_index": 2},
    )

    payload = default_reference_step_dict(step)

    assert payload["action"] == "run-executed"
    assert payload["details"]["chronological_execution_index"] == 2
    assert payload["run_id"] is not None
    assert payload["trial_id"] is not None


def test_generic_analyzer_rejects_a_qualified_default_block(monkeypatch) -> None:
    monkeypatch.setattr(
        default_reference,
        "default_reference_history",
        lambda settings, campaign_id: {
            "block": {"qualification_purpose": "POST_SCREENING_TEMPORAL_STABILITY"},
            "runs": [],
        },
    )

    with pytest.raises(ValueError, match="dedicated analyzer"):
        default_reference.analyze_default_reference(
            Settings.model_construct(),
            uuid.uuid4(),
        )


def test_export_verifies_and_writes_finalized_analysis(
    tmp_path: Path,
    monkeypatch,
) -> None:
    campaign_id = uuid.uuid4()
    block_id = uuid.uuid4()
    hash_payload = {"n": 5, "outcome": "PASSED"}
    analysis_sha256 = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    analysis = {**hash_payload, "analysis_sha256": analysis_sha256}
    monkeypatch.setattr(
        default_reference,
        "_block",
        lambda settings, requested_campaign_id: {
            "block_id": block_id,
            "status": "PASSED",
            "final_analysis": analysis,
            "final_analysis_sha256": analysis_sha256,
        },
    )

    result = default_reference.export_default_reference_analysis(
        Settings.model_construct(artifact_dir=tmp_path),
        campaign_id,
    )

    output = Path(result["output_path"])
    assert output == (
        tmp_path / "default-reference" / f"default-reference-analysis-{block_id}.json"
    )
    assert json.loads(output.read_text(encoding="utf-8")) == analysis
    assert result["analysis_sha256"] == analysis_sha256
