from __future__ import annotations

import uuid

from charmdb.restore.candidate import (
    CandidateRestoreResult,
    FingerprintComparison,
    candidate_restore_result_dict,
    compare_fingerprints,
)


def test_exact_core_mismatch_fails_even_when_physical_tier_passes() -> None:
    result = compare_fingerprints(
        {"snapshot": "expected", "rows": {"accounts": 100}},
        {"database_size_bytes": 1000},
        {"snapshot": "different", "rows": {"accounts": 100}},
        {"database_size_bytes": 1005},
        {"database_size_bytes": {"relative": 0.01, "absolute": 0}},
    )

    assert not result.passed
    assert result.physical_statistics_passed
    assert result.exact_mismatches == {
        "snapshot": {"expected": "expected", "observed": "different"}
    }


def test_physical_numeric_value_passes_inside_larger_tolerance() -> None:
    result = compare_fingerprints(
        {"snapshot": "same"},
        {"database_size_bytes": 1000},
        {"snapshot": "same"},
        {"database_size_bytes": 1015},
        {"database_size_bytes": {"relative": 0.01, "absolute": 20}},
    )

    assert result.passed
    assert result.physical_mismatches == {}


def test_physical_numeric_value_fails_outside_tolerance() -> None:
    result = compare_fingerprints(
        {},
        {"database_size_bytes": 1000},
        {},
        {"database_size_bytes": 1021},
        {"database_size_bytes": {"relative": 0.01, "absolute": 20}},
    )

    assert not result.physical_statistics_passed
    assert result.physical_mismatches["database_size_bytes"]["allowed"] == 20


def test_wildcard_tolerance_matches_relation_names_containing_dots() -> None:
    result = compare_fingerprints(
        {},
        {"statistics": {"public.pgbench_accounts": {"n_live_tup": 1000}}},
        {},
        {"statistics": {"public.pgbench_accounts": {"n_live_tup": 1005}}},
        {"statistics.*.n_live_tup": {"relative": 0.01, "absolute": 0}},
    )

    assert result.passed


def test_missing_physical_field_fails_closed() -> None:
    result = compare_fingerprints(
        {},
        {"statistics": {"accounts": {"n_dead_tup": 0}}},
        {},
        {"statistics": {}},
        {"statistics.*.n_dead_tup": {"relative": 0.05, "absolute": 10}},
    )

    assert not result.physical_statistics_passed
    assert "statistics.accounts.n_dead_tup" in result.physical_mismatches


def test_restore_result_dict_is_json_serializable_shape() -> None:
    ids = [uuid.uuid4() for _ in range(4)]
    result = CandidateRestoreResult(
        ids[0],
        ids[1],
        ids[2],
        ids[3],
        1,
        True,
        FingerprintComparison(True, True, {}, {}),
        1.25,
    )

    payload = candidate_restore_result_dict(result)

    assert [payload[key] for key in ("restore_id", "trial_id", "baseline_id", "validation_id")] == [
        str(value) for value in ids
    ]
    assert payload["comparison"]["exact_core_passed"] is True
