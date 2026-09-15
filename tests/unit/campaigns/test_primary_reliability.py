from __future__ import annotations

import copy
from pathlib import Path

import pytest

from charmdb.campaigns.restore_soak import (
    PrimaryRestoreSoakError,
    primary_restore_soak_summary,
)
from charmdb.optimization.design import (
    PRIMARY_MANIFEST,
    primary_restore_soak_contract_sha256,
)
from charmdb.protocol import load_manifest
from charmdb.restore.candidate import compare_fingerprints

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / PRIMARY_MANIFEST


def test_restore_soak_contract_survives_authorization_metadata_only() -> None:
    payload = load_manifest(MANIFEST).payload
    authorized = copy.deepcopy(payload)
    authorized["status"] = "ready"
    authorized["execution_ready"] = True
    authorized["infrastructure_retry_policy"]["status"] = "frozen"
    authorized["drift_interpretation"]["status"] = "frozen"
    authorized["unresolved_decisions"] = []

    assert primary_restore_soak_contract_sha256(authorized) == primary_restore_soak_contract_sha256(
        payload
    )

    changed_profile = copy.deepcopy(payload)
    changed_profile["benchmark_profile"]["warmup_seconds"] = 599
    assert primary_restore_soak_contract_sha256(
        changed_profile
    ) != primary_restore_soak_contract_sha256(payload)


def test_restore_soak_summary_requires_exact_consecutive_passed_ledger() -> None:
    rows = [
        {
            "sequence": sequence,
            "status": "PASSED",
            "duration_seconds": 100.0 + sequence,
            "restore_validation_id": f"validation-{sequence}",
        }
        for sequence in range(1, 16)
    ]
    summary = primary_restore_soak_summary(rows, 15)

    assert summary["outcome"] == "PASSED"
    assert summary["consecutive_passes"] == 15
    assert summary["duration_summary_seconds"]["minimum"] == 101.0
    assert summary["duration_summary_seconds"]["maximum"] == 115.0

    rows[7]["status"] = "FAILED"
    with pytest.raises(ValueError, match="consecutive passed"):
        primary_restore_soak_summary(rows, 15)


def test_soak_failure_retains_its_fingerprint_diagnostics() -> None:
    comparison = compare_fingerprints(
        {"row_counts": {"public.pgbench_history": 0}},
        {"database_size_bytes": 7_848_900_287},
        {"row_counts": {"public.pgbench_history": 2401}},
        {"database_size_bytes": 7_848_900_287},
        {},
    )
    assert comparison.passed is False

    error = PrimaryRestoreSoakError(
        "primary restore soak failed the frozen fingerprints",
        {
            "exact_core_passed": comparison.exact_core_passed,
            "physical_statistics_passed": comparison.physical_statistics_passed,
            "exact_mismatches": comparison.exact_mismatches,
            "physical_mismatches": comparison.physical_mismatches,
        },
    )

    assert isinstance(error, RuntimeError)
    assert error.details["exact_core_passed"] is False
    assert error.details["exact_mismatches"]["row_counts.public.pgbench_history"] == {
        "expected": 0,
        "observed": 2401,
    }
    assert error.details["physical_mismatches"] == {}
