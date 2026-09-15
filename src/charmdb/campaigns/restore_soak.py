from __future__ import annotations

import hashlib
import json
import statistics
import time
import uuid
from pathlib import Path
from typing import Any, cast

from psycopg.types.json import Jsonb

from charmdb.campaigns.default_reference import (
    FROZEN_BASELINE_ID,
    FROZEN_PREFLIGHT_ID,
    FROZEN_RESTORE_MECHANISM,
)
from charmdb.campaigns.primary import _artifact_disk_headroom, primary_manifest_payload
from charmdb.campaigns.screening_recovery import TARGET_IMAGE_DIGEST, _container_init_evidence
from charmdb.config import Settings
from charmdb.db import connect
from charmdb.metrics import percentile
from charmdb.optimization.design import (
    PRIMARY_MANIFEST,
    primary_restore_soak_contract_sha256,
)
from charmdb.restore.fingerprint import (
    capture_live_fingerprint,
    compare_fingerprints,
    standardize_logical_restore_state,
)
from charmdb.restore.preflight import _target_safety, validate_dataset_restore

PRIMARY_RESTORE_SOAK_STAGE = "primary-restore-reliability"


class PrimaryRestoreSoakError(RuntimeError):
    """A soak repetition failure that carries its own diagnostic evidence.

    The same fingerprint comparison guards every Wave A observation, so a
    failure must record which tier and which fields disagreed rather than only
    that a check failed.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, Any] = dict(details or {})


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _baseline(settings: Settings) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT baseline_id,preflight_id,restore_mechanism,approved,
                      exact_core,physical_statistics,physical_tolerances
               FROM charm_control.experiment_candidate_dataset_baselines
               WHERE baseline_id=%s""",
            (FROZEN_BASELINE_ID,),
        )
        row = cur.fetchone()
    if row is None or row["approved"] is not True:
        raise ValueError("primary restore soak requires the approved frozen baseline")
    if uuid.UUID(str(row["preflight_id"])) != FROZEN_PREFLIGHT_ID:
        raise ValueError("primary restore soak baseline has unexpected preflight lineage")
    if row["restore_mechanism"] != FROZEN_RESTORE_MECHANISM:
        raise ValueError("primary restore soak requires the frozen logical restore")
    return dict(row)


def primary_restore_soak_summary(runs: list[dict[str, Any]], required: int) -> dict[str, Any]:
    if len(runs) != required or required < 15 or required > 20:
        raise ValueError("primary restore soak summary requires its complete 15-20 run ledger")
    if any(row.get("status") != "PASSED" for row in runs):
        raise ValueError("primary restore soak summary requires consecutive passed runs")
    sequences = [int(row["sequence"]) for row in runs]
    if sequences != list(range(1, required + 1)):
        raise ValueError("primary restore soak sequence is incomplete or unordered")
    durations = [float(row["duration_seconds"]) for row in runs]
    return {
        "outcome": "PASSED",
        "required_repetitions": required,
        "consecutive_passes": len(runs),
        "duration_seconds": durations,
        "duration_summary_seconds": {
            "minimum": min(durations),
            "mean": statistics.fmean(durations),
            "median": statistics.median(durations),
            "p95_descriptive_interpolated": percentile(durations, 0.95),
            "maximum": max(durations),
            "total": sum(durations),
        },
        "restore_validation_ids": [str(row["restore_validation_id"]) for row in runs],
    }


def primary_restore_soak_readiness(
    settings: Settings,
    manifest_path: Path = PRIMARY_MANIFEST,
) -> dict[str, Any]:
    manifest_sha256, payload = primary_manifest_payload(manifest_path)
    baseline = _baseline(settings)
    target = _target_safety(settings, require_idle=False)
    artifact_directory = settings.artifact_dir.resolve()
    outside_onedrive = "onedrive" not in str(artifact_directory).lower()
    environment = dict(payload["pre_campaign_environment_gates"])
    disk = _artifact_disk_headroom(
        artifact_directory, int(environment["minimum_artifact_free_bytes"])
    )
    init_evidence = _container_init_evidence()
    init_passed = (
        init_evidence["compose_init"] is True
        and init_evidence["postgres_is_pid_one"] is False
        and init_evidence["image_digest"] == TARGET_IMAGE_DIGEST
    )
    contract_sha256 = primary_restore_soak_contract_sha256(payload)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT to_regclass(
                   'charm_control.experiment_v2_primary_restore_soaks'
               ) IS NOT NULL AS installed"""
        )
        row = cur.fetchone()
        schema_installed = bool(row and row["installed"])
        existing = None
        if schema_installed:
            cur.execute(
                """SELECT restore_soak_id,status,required_repetitions,created_at
                   FROM charm_control.experiment_v2_primary_restore_soaks
                   WHERE status IN ('PLANNED','RUNNING')
                   ORDER BY created_at DESC LIMIT 1"""
            )
            existing = cur.fetchone()
    blockers: list[str] = []
    if not schema_installed:
        blockers.append("migration-038-not-applied")
    if target["active_campaigns"] != 0:
        blockers.append("another-campaign-is-active")
    if target["pending_restart"] != 0:
        blockers.append("target-has-pending-restart")
    if target["managed_indexes"] != 0:
        blockers.append("target-has-managed-indexes")
    if target["active_charm_sessions"] != 0:
        blockers.append("target-has-active-charm-sessions")
    if not outside_onedrive:
        blockers.append("artifact-directory-is-inside-onedrive")
    if not disk["passed"]:
        blockers.append("artifact-disk-headroom-below-50-gib")
    if not init_passed:
        blockers.append("target-init-process-mitigation-not-verified")
    if existing is not None:
        blockers.append("restore-soak-already-active")
    return cast(
        dict[str, Any],
        _json_safe(
            {
                "ready": not blockers,
                "blockers": blockers,
                "manifest_sha256": manifest_sha256,
                "contract_sha256": contract_sha256,
                "preflight_id": FROZEN_PREFLIGHT_ID,
                "baseline_id": baseline["baseline_id"],
                "restore_mechanism": FROZEN_RESTORE_MECHANISM,
                "required_repetitions": int(environment["minimum_consecutive_restore_passes"]),
                "schema_installed": schema_installed,
                "target_safety": target,
                "artifact_directory": {
                    "path": str(artifact_directory),
                    "outside_onedrive": outside_onedrive,
                    "disk": disk,
                },
                "container_init": init_evidence,
                "existing_soak": dict(existing) if existing is not None else None,
            }
        ),
    )


def create_primary_restore_soak(
    settings: Settings,
    repetitions: int = 15,
    manifest_path: Path = PRIMARY_MANIFEST,
) -> uuid.UUID:
    readiness = primary_restore_soak_readiness(settings, manifest_path)
    if repetitions < int(readiness["required_repetitions"]) or repetitions > 20:
        raise ValueError("primary restore soak repetitions must be between 15 and 20")
    if readiness["ready"] is not True:
        raise ValueError(f"primary restore soak is blocked: {readiness['blockers']}")
    restore_soak_id = uuid.uuid4()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_primary_restore_soaks
               (restore_soak_id,protocol_id,evidence_role,manifest_sha256,
                contract_sha256,preflight_id,baseline_id,restore_mechanism,
                required_repetitions,artifact_directory,status)
               VALUES (%s,'thesis-protocol-v2','INFRASTRUCTURE',%s,%s,%s,%s,%s,%s,%s,
                       'PLANNED')""",
            (
                restore_soak_id,
                readiness["manifest_sha256"],
                readiness["contract_sha256"],
                FROZEN_PREFLIGHT_ID,
                FROZEN_BASELINE_ID,
                FROZEN_RESTORE_MECHANISM,
                repetitions,
                readiness["artifact_directory"]["path"],
            ),
        )
        for sequence in range(1, repetitions + 1):
            run_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"charmdb:{restore_soak_id}:primary-restore-soak:{sequence}",
            )
            cur.execute(
                """INSERT INTO charm_control.experiment_v2_primary_restore_soak_runs
                   (restore_soak_run_id,restore_soak_id,sequence,status)
                   VALUES (%s,%s,%s,'PLANNED')""",
                (run_id, restore_soak_id, sequence),
            )
        conn.commit()
    return restore_soak_id


def primary_restore_soak_history(settings: Settings, restore_soak_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_v2_primary_restore_soaks
               WHERE restore_soak_id=%s""",
            (restore_soak_id,),
        )
        block = cur.fetchone()
        cur.execute(
            """SELECT * FROM charm_control.experiment_v2_primary_restore_soak_runs
               WHERE restore_soak_id=%s ORDER BY sequence""",
            (restore_soak_id,),
        )
        runs = [dict(row) for row in cur.fetchall()]
    if block is None:
        raise ValueError(f"unknown primary restore soak {restore_soak_id}")
    return cast(dict[str, Any], _json_safe({"block": dict(block), "runs": runs}))


def run_primary_restore_soak_next(
    settings: Settings,
    restore_soak_id: uuid.UUID,
    manifest_path: Path = PRIMARY_MANIFEST,
) -> dict[str, Any]:
    history = primary_restore_soak_history(settings, restore_soak_id)
    block = history["block"]
    if block["status"] in {"PASSED", "FAILED"}:
        return {
            "action": "already-terminal",
            "restore_soak_id": str(restore_soak_id),
            "status": block["status"],
        }
    _, payload = primary_manifest_payload(manifest_path)
    if block["contract_sha256"] != primary_restore_soak_contract_sha256(payload):
        raise ValueError("primary restore-soak contract differs from the current manifest")
    if Path(block["artifact_directory"]).resolve() != settings.artifact_dir.resolve():
        raise ValueError("primary restore soak must resume on its original artifact directory")
    _target_safety(settings)
    baseline = _baseline(settings)
    init_before = _container_init_evidence()
    if (
        init_before["compose_init"] is not True
        or init_before["postgres_is_pid_one"] is not False
        or init_before["image_digest"] != TARGET_IMAGE_DIGEST
    ):
        raise ValueError("primary restore soak requires the verified init-process mitigation")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        if block["status"] == "PLANNED":
            cur.execute(
                """UPDATE charm_control.experiment_v2_primary_restore_soaks
                   SET status='RUNNING'
                   WHERE restore_soak_id=%s AND status='PLANNED'""",
                (restore_soak_id,),
            )
        cur.execute(
            """SELECT * FROM charm_control.experiment_v2_primary_restore_soak_runs
               WHERE restore_soak_id=%s AND status='PLANNED'
               ORDER BY sequence LIMIT 1 FOR UPDATE""",
            (restore_soak_id,),
        )
        planned = cur.fetchone()
        if planned is not None:
            cur.execute(
                """UPDATE charm_control.experiment_v2_primary_restore_soak_runs
                   SET status='RUNNING',started_at=clock_timestamp()
                   WHERE restore_soak_run_id=%s AND status='PLANNED'""",
                (planned["restore_soak_run_id"],),
            )
        conn.commit()
    if planned is None:
        raise RuntimeError("running primary restore soak has no planned repetition")
    run_id = uuid.UUID(str(planned["restore_soak_run_id"]))
    started = time.monotonic()
    try:
        validation = validate_dataset_restore(settings, FROZEN_PREFLIGHT_ID)
        standardize_logical_restore_state(settings)
        exact_core, physical = capture_live_fingerprint(
            settings, FROZEN_PREFLIGHT_ID, validation.validation_id
        )
        comparison = compare_fingerprints(
            dict(baseline["exact_core"]),
            dict(baseline["physical_statistics"]),
            exact_core,
            physical,
            dict(baseline["physical_tolerances"]),
        )
        init_after = _container_init_evidence()
        if not comparison.passed:
            raise PrimaryRestoreSoakError(
                "primary restore soak failed the frozen fingerprints",
                {
                    "restore_validation_id": str(validation.validation_id),
                    "exact_core_passed": comparison.exact_core_passed,
                    "physical_statistics_passed": comparison.physical_statistics_passed,
                    "exact_mismatches": comparison.exact_mismatches,
                    "physical_mismatches": comparison.physical_mismatches,
                },
            )
        if (
            init_after["compose_init"] is not True
            or init_after["postgres_is_pid_one"] is not False
            or init_after["image_digest"] != TARGET_IMAGE_DIGEST
        ):
            raise PrimaryRestoreSoakError(
                "init-process mitigation changed during primary restore soak",
                {
                    "container_init_before": init_before,
                    "container_init_after": init_after,
                    "expected_image_digest": TARGET_IMAGE_DIGEST,
                },
            )
        duration = time.monotonic() - started
        verification = {
            "validation_id": str(validation.validation_id),
            "exact_core_passed": comparison.exact_core_passed,
            "physical_statistics_passed": comparison.physical_statistics_passed,
            "container_init_before": init_before,
            "container_init_after": init_after,
            "postgresql_version": exact_core["postgres_version"],
            "image_digest": exact_core["image_digest"],
        }
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_primary_restore_soak_runs
                   SET status='PASSED',restore_validation_id=%s,duration_seconds=%s,
                       verification_details=%s,completed_at=clock_timestamp()
                   WHERE restore_soak_run_id=%s AND status='RUNNING'""",
                (validation.validation_id, duration, Jsonb(verification), run_id),
            )
            cur.execute(
                """SELECT restore_soak_run_id,sequence,status,restore_validation_id,
                          duration_seconds,verification_details
                   FROM charm_control.experiment_v2_primary_restore_soak_runs
                   WHERE restore_soak_id=%s AND status='PASSED' ORDER BY sequence""",
                (restore_soak_id,),
            )
            passed = [dict(row) for row in cur.fetchall()]
            required = int(block["required_repetitions"])
            if len(passed) == required:
                result = {
                    "restore_soak_id": str(restore_soak_id),
                    "contract_sha256": block["contract_sha256"],
                    "preflight_id": str(FROZEN_PREFLIGHT_ID),
                    "baseline_id": str(FROZEN_BASELINE_ID),
                    "artifact_directory": str(settings.artifact_dir.resolve()),
                    **primary_restore_soak_summary(passed, required),
                }
                cur.execute(
                    """UPDATE charm_control.experiment_v2_primary_restore_soaks
                       SET status='PASSED',result=%s,result_sha256=%s,
                           completed_at=clock_timestamp()
                       WHERE restore_soak_id=%s AND status='RUNNING'""",
                    (Jsonb(result), _canonical_sha256(result), restore_soak_id),
                )
            conn.commit()
        return {
            "action": "soak-complete" if len(passed) == required else "repetition-passed",
            "restore_soak_id": str(restore_soak_id),
            "restore_soak_run_id": str(run_id),
            "sequence": int(planned["sequence"]),
            "duration_seconds": duration,
            "completed_repetitions": len(passed),
            "required_repetitions": required,
            "status": "PASSED" if len(passed) == required else "RUNNING",
        }
    except Exception as error:
        duration = time.monotonic() - started
        failure: dict[str, Any] = {"error_type": type(error).__name__, "message": str(error)}
        diagnostics = getattr(error, "details", None)
        if isinstance(diagnostics, dict) and diagnostics:
            failure["diagnostics"] = _json_safe(diagnostics)
        result = {
            "restore_soak_id": str(restore_soak_id),
            "contract_sha256": block["contract_sha256"],
            "outcome": "FAILED",
            "failed_sequence": int(planned["sequence"]),
            "failure": failure,
        }
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_primary_restore_soak_runs
                   SET status='FAILED',duration_seconds=%s,failure_details=%s,
                       completed_at=clock_timestamp()
                   WHERE restore_soak_run_id=%s AND status='RUNNING'""",
                (duration, Jsonb(failure), run_id),
            )
            cur.execute(
                """UPDATE charm_control.experiment_v2_primary_restore_soaks
                   SET status='FAILED',result=%s,result_sha256=%s,
                       completed_at=clock_timestamp()
                   WHERE restore_soak_id=%s AND status='RUNNING'""",
                (Jsonb(result), _canonical_sha256(result), restore_soak_id),
            )
            conn.commit()
        raise
