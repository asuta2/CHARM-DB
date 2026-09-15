from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.db import connect
from charmdb.protocol import EVIDENCE_ROLES, PROTOCOL_ID
from charmdb.restore.fingerprint import (
    DEFAULT_PHYSICAL_TOLERANCES,
    PGBENCH_RELATIONS,
    _target_image_digest,
    capture_live_fingerprint,
    compare_fingerprints,
    standardize_logical_restore_state,
)
from charmdb.restore.models import (
    CandidateBaseline,
    CandidateRestoreResult,
    FingerprintComparison,
)

RESTORE_MECHANISMS = frozenset({"copy-on-write", "physical-archive", "logical-restore"})
EXECUTED_EVIDENCE_ROLES = EVIDENCE_ROLES - {"HISTORICAL_DIAGNOSTIC"}


def capture_pre_restore_fingerprint(settings: Settings) -> dict[str, Any]:
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        relation_rows: dict[str, int | None] = {}
        for relation in PGBENCH_RELATIONS:
            schema_name, relation_name = relation.split(".", 1)
            cur.execute("SELECT to_regclass(%s) AS relation", (relation,))
            if cur.fetchone()["relation"] is None:  # type: ignore[index]
                relation_rows[relation] = None
                continue
            cur.execute(
                sql.SQL("SELECT count(*)::bigint AS count FROM {}.{}").format(
                    sql.Identifier(schema_name), sql.Identifier(relation_name)
                )
            )
            relation_rows[relation] = int(cur.fetchone()["count"])  # type: ignore[index]
        cur.execute(
            """SELECT current_database() AS database,
                      current_setting('server_version') AS postgres_version,
                      pg_database_size(current_database())::bigint AS database_size_bytes"""
        )
        identity = dict(cur.fetchone() or {})
    return {
        "relation_rows": relation_rows,
        "database": identity.get("database"),
        "postgres_version": identity.get("postgres_version"),
        "database_size_bytes": identity.get("database_size_bytes"),
        "image_digest": _target_image_digest(),
    }


def register_candidate_baseline(
    settings: Settings,
    preflight_id: uuid.UUID,
    validation_id: uuid.UUID,
    *,
    restore_mechanism: str = "logical-restore",
    physical_tolerances: dict[str, dict[str, float]] | None = None,
    approve: bool = False,
    details: dict[str, Any] | None = None,
) -> CandidateBaseline:
    if restore_mechanism not in RESTORE_MECHANISMS:
        raise ValueError(f"unsupported restore mechanism {restore_mechanism}")
    if restore_mechanism == "logical-restore":
        standardize_logical_restore_state(settings)
    exact_core, physical = capture_live_fingerprint(settings, preflight_id, validation_id)
    tolerances = physical_tolerances or DEFAULT_PHYSICAL_TOLERANCES
    baseline_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:v2:baseline:{preflight_id}")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_candidate_dataset_baselines
               (baseline_id,preflight_id,validation_id,restore_mechanism,exact_core,
                physical_statistics,physical_tolerances,approved,details,approved_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       CASE WHEN %s THEN clock_timestamp() ELSE NULL END)
               ON CONFLICT (preflight_id) DO NOTHING""",
            (
                baseline_id,
                preflight_id,
                validation_id,
                restore_mechanism,
                Jsonb(exact_core),
                Jsonb(physical),
                Jsonb(tolerances),
                approve,
                Jsonb(details or {}),
                approve,
            ),
        )
        cur.execute(
            """SELECT baseline_id,preflight_id,validation_id,restore_mechanism,approved,
                      exact_core,physical_statistics,physical_tolerances
               FROM charm_control.experiment_candidate_dataset_baselines
               WHERE preflight_id=%s""",
            (preflight_id,),
        )
        row = cur.fetchone()
        conn.commit()
    if row is None:
        raise RuntimeError("candidate baseline persistence failed")
    if dict(row["exact_core"]) != exact_core:
        raise ValueError("an existing candidate baseline differs from the live fingerprint")
    return CandidateBaseline(
        uuid.UUID(str(row["baseline_id"])),
        uuid.UUID(str(row["preflight_id"])),
        uuid.UUID(str(row["validation_id"])),
        str(row["restore_mechanism"]),
        bool(row["approved"]),
        dict(row["exact_core"]),
        dict(row["physical_statistics"]),
        dict(row["physical_tolerances"]),
    )


def approve_candidate_baseline(
    settings: Settings, baseline_id: uuid.UUID, reason: str
) -> CandidateBaseline:
    if not reason.strip():
        raise ValueError("baseline approval requires a reason")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_candidate_dataset_baselines
               SET approved=true,approved_at=clock_timestamp(),
                   details=details || %s
               WHERE baseline_id=%s AND NOT approved""",
            (Jsonb({"approval_reason": reason}), baseline_id),
        )
        cur.execute(
            """SELECT baseline_id,preflight_id,validation_id,restore_mechanism,approved,
                      exact_core,physical_statistics,physical_tolerances
               FROM charm_control.experiment_candidate_dataset_baselines
               WHERE baseline_id=%s""",
            (baseline_id,),
        )
        row = cur.fetchone()
        conn.commit()
    if row is None or not bool(row["approved"]):
        raise ValueError("unknown or unapproved candidate baseline")
    return CandidateBaseline(
        uuid.UUID(str(row["baseline_id"])),
        uuid.UUID(str(row["preflight_id"])),
        uuid.UUID(str(row["validation_id"])),
        str(row["restore_mechanism"]),
        True,
        dict(row["exact_core"]),
        dict(row["physical_statistics"]),
        dict(row["physical_tolerances"]),
    )


def _load_trial_and_baseline(
    settings: Settings, trial_id: uuid.UUID, preflight_id: uuid.UUID
) -> tuple[dict[str, Any], CandidateBaseline]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT trial_id,campaign_id,protocol_id,evidence_role,workflow_payload,
                      candidate_restore_required,candidate_dataset_restore_id
               FROM charm_control.trials WHERE trial_id=%s""",
            (trial_id,),
        )
        trial_row = cur.fetchone()
        cur.execute(
            """SELECT baseline_id,preflight_id,validation_id,restore_mechanism,approved,
                      exact_core,physical_statistics,physical_tolerances
               FROM charm_control.experiment_candidate_dataset_baselines
               WHERE preflight_id=%s""",
            (preflight_id,),
        )
        baseline_row = cur.fetchone()
    if trial_row is None:
        raise ValueError(f"unknown candidate restore trial {trial_id}")
    trial = dict(trial_row)
    if (
        trial["protocol_id"] != PROTOCOL_ID
        or str(trial["evidence_role"]) not in EXECUTED_EVIDENCE_ROLES
        or not bool(trial["candidate_restore_required"])
    ):
        raise ValueError("trial is not eligible for a protocol-v2 candidate restore")
    payload = dict(trial["workflow_payload"] or {})
    if str(payload.get("preflight_id")) != str(preflight_id):
        raise ValueError("trial preflight lineage does not match the requested restore")
    if baseline_row is None or not bool(baseline_row["approved"]):
        raise ValueError("candidate restore requires an approved baseline")
    baseline = CandidateBaseline(
        uuid.UUID(str(baseline_row["baseline_id"])),
        uuid.UUID(str(baseline_row["preflight_id"])),
        uuid.UUID(str(baseline_row["validation_id"])),
        str(baseline_row["restore_mechanism"]),
        True,
        dict(baseline_row["exact_core"]),
        dict(baseline_row["physical_statistics"]),
        dict(baseline_row["physical_tolerances"]),
    )
    return trial, baseline


def _comparison_from_row(row: dict[str, Any]) -> FingerprintComparison:
    details = dict(row.get("verification_details") or {})
    return FingerprintComparison(
        bool(row.get("exact_core_passed")),
        bool(row.get("physical_statistics_passed")),
        dict(details.get("exact_mismatches") or {}),
        dict(details.get("physical_mismatches") or {}),
    )


def _ensure_candidate_dataset_restored_locked(
    settings: Settings,
    trial_id: uuid.UUID,
    preflight_id: uuid.UUID,
    *,
    experiment_arm_id: uuid.UUID | None = None,
    budget_position: int | None = None,
    restore_mechanism: str = "logical-restore",
    physical_archive_id: uuid.UUID | None = None,
) -> CandidateRestoreResult:
    if restore_mechanism not in {"logical-restore", "physical-archive"}:
        raise NotImplementedError(
            f"candidate restore mechanism {restore_mechanism!r} is ineligible"
        )
    if (restore_mechanism == "physical-archive") != (physical_archive_id is not None):
        raise ValueError("physical-archive restore requires exactly one physical_archive_id")
    trial, baseline = _load_trial_and_baseline(settings, trial_id, preflight_id)
    physical_archive = None
    if physical_archive_id is not None:
        from charmdb.restore.physical_archive import load_physical_archive

        # Read lineage metadata here; the artifact size/SHA-256 check belongs to
        # restore_physical_archive so it is included in the measured duration.
        physical_archive = load_physical_archive(
            settings,
            physical_archive_id,
            verify_artifact=False,
        )
        if (
            physical_archive.baseline_id != baseline.baseline_id
            or physical_archive.preflight_id != preflight_id
        ):
            raise ValueError("physical archive lineage differs from the trial baseline")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_candidate_dataset_restores
               WHERE trial_id=%s ORDER BY attempt DESC FOR UPDATE""",
            (trial_id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
        if rows and rows[0]["status"] == "PASSED":
            row = rows[0]
            if row["restore_mechanism"] != restore_mechanism:
                raise ValueError("passed restore mechanism differs from the requested mechanism")
            return CandidateRestoreResult(
                uuid.UUID(str(row["restore_id"])),
                trial_id,
                baseline.baseline_id,
                uuid.UUID(str(row["validation_id"])),
                int(row["attempt"]),
                False,
                _comparison_from_row(row),
                float(row["duration_seconds"]),
            )
        retry_of = None
        recovery_root = None
        if rows:
            latest = rows[0]
            retry_of = uuid.UUID(str(latest["restore_id"]))
            recovery_root = uuid.UUID(str(latest["recovery_root_id"]))
            if latest["status"] in {"RESTORING", "VERIFYING"}:
                cur.execute(
                    """UPDATE charm_control.experiment_candidate_dataset_restores
                       SET status='INTERRUPTED',completed_at=clock_timestamp(),
                           duration_seconds=EXTRACT(EPOCH FROM clock_timestamp()-started_at),
                           failure_details=%s WHERE restore_id=%s""",
                    (Jsonb({"reason": "resumed after interrupted restore"}), retry_of),
                )
        attempt = int(rows[0]["attempt"]) + 1 if rows else 1
        restore_id = uuid.uuid4()
        recovery_root = recovery_root or restore_id
        restore_source = (
            physical_archive.archive_sha256
            if physical_archive is not None
            else str(baseline.exact_core["snapshot_sha256"])
        )
        image_digest = (
            physical_archive.image_digest
            if physical_archive is not None
            else str(baseline.exact_core["image_digest"])
        )
        cur.execute(
            """INSERT INTO charm_control.experiment_candidate_dataset_restores
               (restore_id,trial_id,campaign_id,experiment_arm_id,budget_position,
                protocol_id,evidence_role,baseline_id,preflight_id,restore_mechanism,
                restore_source,snapshot_sha256,image_digest,status,attempt,idempotency_key,
                retry_of_restore_id,recovery_root_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'REQUESTED',%s,%s,%s,%s)""",
            (
                restore_id,
                trial_id,
                trial["campaign_id"],
                experiment_arm_id,
                budget_position,
                PROTOCOL_ID,
                trial["evidence_role"],
                baseline.baseline_id,
                preflight_id,
                restore_mechanism,
                restore_source,
                baseline.exact_core["snapshot_sha256"],
                image_digest,
                attempt,
                f"candidate-restore:{trial_id}",
                retry_of,
                recovery_root,
            ),
        )
        cur.execute(
            """UPDATE charm_control.experiment_candidate_dataset_restores
               SET status='RESTORING',started_at=clock_timestamp()
               WHERE restore_id=%s""",
            (restore_id,),
        )
        conn.commit()
    started = time.monotonic()
    try:
        try:
            pre_restore = capture_pre_restore_fingerprint(settings)
        except Exception:
            if restore_mechanism != "physical-archive":
                raise
            from charmdb.restore.physical_archive import stopped_target_fingerprint

            pre_restore = stopped_target_fingerprint()
            if bool(pre_restore["target_available"]):
                raise
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_candidate_dataset_restores
                   SET pre_restore_fingerprint=%s WHERE restore_id=%s AND status='RESTORING'""",
                (Jsonb(pre_restore), restore_id),
            )
            conn.commit()
        campaign_id = uuid.UUID(str(trial["campaign_id"]))
        if restore_mechanism == "logical-restore":
            # Import lazily because the v1 preflight module reaches the durable worker
            # through experiment_execution during module initialization.
            from charmdb.restore.preflight import validate_dataset_restore

            validation = validate_dataset_restore(
                settings,
                preflight_id,
                permitted_campaign_id=campaign_id,
            )
            standardize_logical_restore_state(settings)
        else:
            from charmdb.restore.physical_archive import restore_physical_archive

            if physical_archive_id is None:
                raise RuntimeError("physical archive identifier was lost before restore")
            validation = restore_physical_archive(
                settings,
                physical_archive_id,
                permitted_campaign_id=campaign_id,
            )
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_candidate_dataset_restores
                   SET status='VERIFYING',validation_id=%s WHERE restore_id=%s""",
                (validation.validation_id, restore_id),
            )
            conn.commit()
        exact_core, physical = capture_live_fingerprint(
            settings, preflight_id, validation.validation_id
        )
        comparison = compare_fingerprints(
            baseline.exact_core,
            baseline.physical_statistics,
            exact_core,
            physical,
            baseline.physical_tolerances,
        )
        duration = time.monotonic() - started
        status = "PASSED" if comparison.passed else "FAILED"
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_candidate_dataset_restores
                   SET status=%s,completed_at=clock_timestamp(),duration_seconds=%s,
                       post_restore_fingerprint=%s,exact_core_passed=%s,
                       physical_statistics_passed=%s,verification_details=%s
                   WHERE restore_id=%s""",
                (
                    status,
                    duration,
                    Jsonb({"exact_core": exact_core, "physical_statistics": physical}),
                    comparison.exact_core_passed,
                    comparison.physical_statistics_passed,
                    Jsonb(
                        {
                            "exact_mismatches": comparison.exact_mismatches,
                            "physical_mismatches": comparison.physical_mismatches,
                        }
                    ),
                    restore_id,
                ),
            )
            if comparison.passed:
                cur.execute(
                    """UPDATE charm_control.trials SET candidate_dataset_restore_id=%s
                       WHERE trial_id=%s AND candidate_dataset_restore_id IS NULL""",
                    (restore_id, trial_id),
                )
            conn.commit()
        if not comparison.passed:
            raise RuntimeError("candidate dataset baseline fingerprint verification failed")
        return CandidateRestoreResult(
            restore_id,
            trial_id,
            baseline.baseline_id,
            validation.validation_id,
            attempt,
            True,
            comparison,
            duration,
        )
    except Exception as error:
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_candidate_dataset_restores
                   SET status='FAILED',started_at=COALESCE(started_at,clock_timestamp()),
                       completed_at=clock_timestamp(),
                       duration_seconds=EXTRACT(EPOCH FROM clock_timestamp()-
                           COALESCE(started_at,clock_timestamp())),
                       failure_details=%s
                   WHERE restore_id=%s AND status IN ('REQUESTED','RESTORING','VERIFYING')""",
                (Jsonb({"error_type": type(error).__name__, "message": str(error)}), restore_id),
            )
            conn.commit()
        raise


def ensure_candidate_dataset_restored(
    settings: Settings,
    trial_id: uuid.UUID,
    preflight_id: uuid.UUID,
    *,
    experiment_arm_id: uuid.UUID | None = None,
    budget_position: int | None = None,
    restore_mechanism: str = "logical-restore",
    physical_archive_id: uuid.UUID | None = None,
) -> CandidateRestoreResult:
    """Serialize restore execution for a trial across worker and operator processes."""
    with connect(settings.control_dsn) as lock_conn:
        lock_conn.autocommit = True
        with lock_conn.cursor() as lock_cur:
            lock_cur.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (str(trial_id),))
            try:
                return _ensure_candidate_dataset_restored_locked(
                    settings,
                    trial_id,
                    preflight_id,
                    experiment_arm_id=experiment_arm_id,
                    budget_position=budget_position,
                    restore_mechanism=restore_mechanism,
                    physical_archive_id=physical_archive_id,
                )
            finally:
                lock_cur.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s,0))", (str(trial_id),)
                )


def verify_trial_candidate_restore(settings: Settings, trial_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT t.candidate_restore_required,t.candidate_dataset_restore_id,
                      r.restore_id,r.trial_id AS restore_trial_id,r.status,r.preflight_id,
                      r.exact_core_passed,r.physical_statistics_passed,r.duration_seconds
               FROM charm_control.trials t
               LEFT JOIN charm_control.experiment_candidate_dataset_restores r
                 ON r.restore_id=t.candidate_dataset_restore_id
               WHERE t.trial_id=%s""",
            (trial_id,),
        )
        row = cur.fetchone()
    if (
        row is None
        or not bool(row["candidate_restore_required"])
        or row["restore_id"] is None
        or row["restore_trial_id"] != trial_id
        or row["status"] != "PASSED"
        or not bool(row["exact_core_passed"])
        or not bool(row["physical_statistics_passed"])
    ):
        raise RuntimeError("trial has no passed candidate-level dataset restore")
    return {
        "restore_id": str(row["restore_id"]),
        "preflight_id": str(row["preflight_id"]),
        "status": str(row["status"]),
        "duration_seconds": float(row["duration_seconds"]),
    }


def candidate_restore_history(
    settings: Settings,
    trial_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 1000:
        raise ValueError("candidate restore history limit must be between 1 and 1000")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT restore_id,trial_id,campaign_id,experiment_arm_id,budget_position,
                      protocol_id,evidence_role,baseline_id,preflight_id,validation_id,
                      restore_mechanism,restore_source,snapshot_sha256,image_digest,status,
                      attempt,idempotency_key,retry_of_restore_id,recovery_root_id,started_at,
                      completed_at,duration_seconds,pre_restore_fingerprint,
                      post_restore_fingerprint,exact_core_passed,
                      physical_statistics_passed,verification_details,failure_details,created_at
               FROM charm_control.experiment_candidate_dataset_restores
               WHERE (%s::uuid IS NULL OR trial_id=%s)
               ORDER BY created_at DESC LIMIT %s""",
            (trial_id, trial_id, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def candidate_restore_result_dict(result: CandidateRestoreResult) -> dict[str, Any]:
    payload = asdict(result)
    for key in ("restore_id", "trial_id", "baseline_id", "validation_id"):
        payload[key] = str(payload[key])
    return payload
