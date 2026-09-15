from __future__ import annotations

import uuid
from pathlib import Path

from charmdb.campaigns.screening import screening_initial_schedule, screening_manifest_payload
from charmdb.campaigns.screening_recovery import (
    RECOVERY_SCHEDULE_SHA256,
    _recovery_manifest_for_sha256,
    recovery_design_summary,
    recovery_manifest_payload,
    recovery_plan_rows,
)

ROOT = Path(__file__).resolve().parents[3]
RECOVERY_MANIFEST = ROOT / "experiments/thesis/manifests/parameter-screening-recovery.json"
SCREENING_MANIFEST = ROOT / "experiments/thesis/manifests/parameter-screening.json"
REMEDIATION_MANIFEST = (
    ROOT / "experiments/thesis/manifests/parameter-screening-recovery-remediation.json"
)


def test_recovery_manifest_freezes_exact_source_tail_and_runtime_bound() -> None:
    digest, payload = recovery_manifest_payload(RECOVERY_MANIFEST)
    _, screening = screening_manifest_payload(SCREENING_MANIFEST)
    expected = [
        item for item in screening_initial_schedule(screening) if item["position"] in (34, 35)
    ]
    summary = recovery_design_summary(RECOVERY_MANIFEST)

    assert len(digest) == 64
    assert payload["recovery_schedule"]["observations"] == expected
    assert payload["recovery_schedule"]["schedule_sha256"] == RECOVERY_SCHEDULE_SHA256
    assert summary["restore_validation_repetitions"] == 3
    assert summary["recovery_observations"] == 2
    assert summary["maximum_oat_observations"] == 6
    assert summary["unresolved_decisions"] == []


def test_recovery_plan_is_deterministic_and_preserves_failed_tail_lineage() -> None:
    _, payload = recovery_manifest_payload(RECOVERY_MANIFEST)
    block_id = uuid.uuid4()
    campaign_id = uuid.uuid4()
    source_runs = [
        {"run_id": uuid.uuid4(), "sequence": 34, "status": "FAILED"},
        {"run_id": uuid.uuid4(), "sequence": 35, "status": "PLANNED"},
    ]

    rows = recovery_plan_rows(payload, block_id, campaign_id, source_runs)
    repeated = recovery_plan_rows(payload, block_id, campaign_id, source_runs)

    assert rows == repeated
    assert [row["sequence"] for row in rows] == [34, 35]
    assert [row["source_run_id"] for row in rows] == [
        source_runs[0]["run_id"],
        source_runs[1]["run_id"],
    ]
    assert [row["evaluation_kind"] for row in rows] == ["SOBOL", "DEFAULT_CONTROL"]
    assert all(row["phase"] == "INITIAL" and row["status"] == "PLANNED" for row in rows)


def test_remediation_manifest_freezes_failed_d031_and_non_manual_cleanup() -> None:
    digest, payload = recovery_manifest_payload(REMEDIATION_MANIFEST)

    assert len(digest) == 64
    assert payload["stage"] == "parameter-screening-recovery-remediation"
    assert payload["supersedes"]["validation_block_id"] == ("ba314a66-9d74-5dec-b50a-1b719f356ce4")
    assert payload["remediation"]["expected_orphan_filenodes"] == [291710, 291725]
    assert payload["remediation"]["expected_orphan_bytes"] == 7_777_845_248
    assert payload["remediation"]["manual_file_deletion_permitted"] is False
    assert payload["recovery_schedule"]["schedule_sha256"] == RECOVERY_SCHEDULE_SHA256


def test_recovery_manifest_is_resolved_from_persisted_campaign_hash() -> None:
    digest, payload = recovery_manifest_payload(REMEDIATION_MANIFEST)

    resolved_digest, resolved_payload, resolved_path = _recovery_manifest_for_sha256(digest)

    assert resolved_digest == digest
    assert resolved_payload == payload
    assert resolved_path == Path(
        "experiments/thesis/manifests/parameter-screening-recovery-remediation.json"
    )
