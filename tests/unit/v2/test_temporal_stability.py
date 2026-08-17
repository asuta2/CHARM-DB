from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from charmdb.v2.temporal_stability import (
    PRIMARY_SEEDS,
    RESERVED_SEEDS,
    SOURCE_ANALYSIS_SHA256,
    _operator_gap_summary,
    create_temporal_stability_plan,
    temporal_stability_design_summary,
    temporal_stability_manifest_payload,
)

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "v2/config/post-screening-temporal-stability.json"


def _seed(index: int) -> int:
    label = f"thesis-protocol-v2/post-screening-temporal-stability/{index}"
    return int.from_bytes(hashlib.sha256(label.encode()).digest()[:4], "big") % 2_147_483_647


def test_manifest_freezes_a_fresh_fixed_five_run_decision() -> None:
    digest, payload = temporal_stability_manifest_payload(MANIFEST)
    summary = temporal_stability_design_summary(MANIFEST)

    assert len(digest) == 64
    assert tuple(_seed(index) for index in range(1, 6)) == PRIMARY_SEEDS
    assert tuple(_seed(index) for index in range(6, 11)) == RESERVED_SEEDS
    assert set(PRIMARY_SEEDS).isdisjoint(RESERVED_SEEDS)
    assert payload["design"]["no_contingency_or_extension"] is True
    assert payload["acceptance_criteria"]["maximum_total_observations"] == 5
    assert payload["purpose"]["does_not_salvage_or_reinterpret_d033"] is True
    assert payload["purpose"]["does_not_authorize_primary_comparison"] is True
    assert payload["status"] == "blocked"
    assert payload["execution_ready"] is False
    assert payload["retirement"]["retired_without_execution"] is True
    assert payload["retirement"]["permanently_prohibit_launch"] is True
    assert summary["source_analysis_sha256"] == SOURCE_ANALYSIS_SHA256
    assert summary["restore_inclusive_hours"] == 2.311


def test_retired_temporal_stability_plan_cannot_be_created() -> None:
    with pytest.raises(ValueError, match="retired without execution"):
        create_temporal_stability_plan(object(), MANIFEST)  # type: ignore[arg-type]


def _gap_rows(gap_minutes: float) -> list[dict[str, object]]:
    first_start = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
    first_end = first_start + timedelta(minutes=20)
    second_start = first_end + timedelta(minutes=gap_minutes)
    return [
        {
            "sequence": 1,
            "chronological_execution_index": 1,
            "trial_started_at": first_start,
            "trial_completed_at": first_end,
        },
        {
            "sequence": 2,
            "chronological_execution_index": 2,
            "trial_started_at": second_start,
            "trial_completed_at": second_start + timedelta(minutes=20),
        },
    ]


def test_operator_gap_accepts_the_pre_registered_boundary() -> None:
    result = _operator_gap_summary(_gap_rows(15.0))

    assert result["passed"] is True
    assert result["maximum_observed_minutes"] == 15.0


def test_operator_gap_blocks_a_late_restart() -> None:
    result = _operator_gap_summary(_gap_rows(15.01))

    assert result["passed"] is False
    assert result["maximum_observed_minutes"] == 15.01
