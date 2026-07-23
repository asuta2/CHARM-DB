import uuid
from dataclasses import replace

import pytest

from charmdb.transfer import (
    CompatibleHistory,
    HistoryRecord,
    TransferOutcome,
    classify_negative_transfer,
    compatible_history,
    default_compatibility,
    warm_start_candidates,
)


def _record(
    throughput: float,
    configuration: dict[str, str],
    vector: tuple[float, ...] = (0.0,) * 11,
) -> HistoryRecord:
    return HistoryRecord(
        uuid.uuid4(),
        uuid.uuid4(),
        default_compatibility(18, "schema", "4cpu-4gib"),
        configuration,
        throughput,
        12.0,
        True,
        vector,
    )


def test_compatibility_gate_rejects_exact_mismatch_and_distant_context() -> None:
    target = default_compatibility(18, "schema", "4cpu-4gib")
    valid = _record(
        500,
        {"random_page_cost": "2", "work_mem": "4096", "effective_io_concurrency": "50"},
    )
    wrong_schema = replace(
        valid, history_id=uuid.uuid4(), compatibility=replace(target, schema_signature="other")
    )
    distant = replace(valid, history_id=uuid.uuid4(), fingerprint_vector=(2.0,) + (0.0,) * 10)
    accepted, rejected = compatible_history(
        [valid, wrong_schema, distant], target, (0.0,) * 11, similarity_threshold=0.5
    )
    assert [item.record.history_id for item in accepted] == [valid.history_id]
    assert rejected == {"incompatible_schema_signature": 1, "fingerprint_distance": 1}


def test_warm_start_methods_preserve_equal_unique_budget() -> None:
    first = _record(
        520,
        {"random_page_cost": "2", "work_mem": "4096", "effective_io_concurrency": "50"},
    )
    second = _record(
        500,
        {"random_page_cost": "3", "work_mem": "8192", "effective_io_concurrency": "100"},
    )
    history = [CompatibleHistory(first, 0.1, 0.9), CompatibleHistory(second, 0.2, 0.8)]
    for method in (
        "no_transfer",
        "historical_champion",
        "similarity_weighted",
        "rank_weighted_ensemble",
    ):
        candidates = warm_start_candidates(method, history, budget=4, seed=42)
        assert len(candidates) == 4
        assert len({tuple(item.configuration.items()) for item in candidates}) == 4
    champion = warm_start_candidates("historical_champion", history, budget=2, seed=42)
    assert champion[0].configuration == first.configuration


def test_negative_transfer_retains_performance_recovery_and_safety_reasons() -> None:
    baseline = TransferOutcome("no_transfer", 8, 500.0, 3, 0)
    transferred = TransferOutcome("historical_champion", 8, 480.0, 5, 1)
    result = classify_negative_transfer(transferred, baseline)
    assert result.negative
    assert result.reasons == (
        "best feasible throughput below practical margin",
        "slower recovery than no transfer",
        "more unsafe trials than no transfer",
    )


def test_negative_transfer_rejects_unequal_realized_budgets() -> None:
    with pytest.raises(ValueError, match="equal realized budgets"):
        classify_negative_transfer(
            TransferOutcome("historical_champion", 7, 500.0, 3, 0),
            TransferOutcome("no_transfer", 8, 500.0, 3, 0),
        )
