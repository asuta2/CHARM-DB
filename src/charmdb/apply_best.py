from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.controller import (
    apply_configuration,
    discover_knobs,
    rollback_configuration,
    settings_equivalent,
    validate_candidate,
)
from charmdb.db import connect
from charmdb.restore.preflight import _target_safety

APPLY_BEST_MANIFEST = Path("experiments/thesis/manifests/apply-best.json")
APPLY_BEST_STAGE = "apply-best"
APPLY_BEST_EVIDENCE_ROLE = "DEPLOYMENT_CONTROL"


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_best_manifest_payload(
    manifest_path: Path = APPLY_BEST_MANIFEST,
) -> tuple[str, dict[str, Any]]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid apply-best JSON in {manifest_path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("apply-best manifest must be a JSON object")
    if (
        payload.get("protocol_id") != "thesis-protocol-v2"
        or payload.get("schema_version") != 1
        or payload.get("stage") != APPLY_BEST_STAGE
        or payload.get("evidence_role") != APPLY_BEST_EVIDENCE_ROLE
    ):
        raise ValueError("apply-best manifest has the wrong stage or evidence role")
    validate_apply_best_manifest(payload)
    return _file_sha256(manifest_path), payload


def champion_configuration(payload: Mapping[str, Any]) -> dict[str, str]:
    champion = dict(payload["champion"])
    return {
        str(name): str(value) for name, value in dict(champion["requested_configuration"]).items()
    }


def validate_apply_best_manifest(payload: Mapping[str, Any]) -> None:
    if payload.get("status") != "blocked":
        raise ValueError("apply-best manifest is not ready for recovery validation")
    if payload.get("recovery_validation_authorized") is not True:
        raise ValueError("apply-best recovery validation is not authorized")
    if payload.get("persistent_activation_authorized") is not False:
        raise ValueError("apply-best manifest must not authorize persistent activation")
    champion = dict(payload.get("champion") or {})
    if (
        champion.get("treatment") != "E"
        or champion.get("selection") != "CONFIRMED_CHAMPION"
        or champion.get("source_primary_run_id") != "e6d70fab-0c90-5795-96a1-5021bff1194c"
    ):
        raise ValueError("apply-best manifest does not identify the frozen F4 champion E")
    expected_knobs = {
        "checkpoint_completion_target",
        "checkpoint_timeout",
        "effective_cache_size",
        "max_parallel_workers_per_gather",
        "max_wal_size",
        "random_page_cost",
        "shared_buffers",
        "work_mem",
    }
    configuration = champion_configuration(payload)
    if set(configuration) != expected_knobs:
        raise ValueError("apply-best champion must contain the frozen eight-knob vector")
    recovery = dict(payload.get("recovery_contract") or {})
    required_recovery = {
        "snapshot_before_change",
        "force_postgresql_restart",
        "verify_requested_equals_active",
        "verify_postgresql_health",
        "verify_no_pending_restart",
        "verify_no_managed_indexes",
        "verify_no_charm_sessions",
        "rollback_to_exact_snapshot",
        "verify_snapshot_equals_active_after_rollback",
        "single_recovery_cycle_required",
    }
    if any(recovery.get(name) is not True for name in required_recovery):
        raise ValueError("apply-best recovery contract is incomplete")
    authorization = dict(payload.get("authorization_contract") or {})
    if (
        authorization.get("persist_decision_before_activation") is not True
        or authorization.get("manifest_does_not_authorize_activation") is not True
        or authorization.get("activation_requires_recovery_tested_state") is not True
        or authorization.get("rollback_command_available_while_active") is not True
    ):
        raise ValueError("apply-best activation contract is incomplete")
    required_fields = set(authorization.get("required_fields") or [])
    if required_fields != {
        "decision_id",
        "actor",
        "statement",
        "confirmed_configuration_sha256",
    }:
        raise ValueError("apply-best activation decision fields differ from the frozen contract")


def _json_file_gate(path: Path, expected_payload_sha256: str) -> dict[str, Any]:
    if not path.is_file():
        return {"passed": False, "path": str(path), "reason": "missing"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {"passed": False, "path": str(path), "reason": str(error)}
    actual = str(payload.get("analysis_sha256") or "")
    return {
        "passed": actual == expected_payload_sha256,
        "path": str(path),
        "file_sha256": _file_sha256(path),
        "analysis_sha256": actual,
    }


def _source_gate(settings: Settings, payload: Mapping[str, Any]) -> dict[str, Any]:
    source = dict(payload["source_evidence"])
    f4_campaign_id = uuid.UUID(str(source["f4_campaign_id"]))
    wave_b_campaign_id = uuid.UUID(str(source["wave_b_campaign_id"]))
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT b.f4_block_id,b.status,c.status AS campaign_status,
                      b.manifest_sha256,b.analysis_sha256,b.analysis
               FROM charm_control.experiment_v2_f4_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.campaign_id=%s""",
            (f4_campaign_id,),
        )
        f4 = cur.fetchone()
        cur.execute(
            """SELECT b.primary_block_id,b.status,c.status AS campaign_status,
                      b.analysis_sha256
               FROM charm_control.experiment_v2_primary_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.campaign_id=%s AND b.wave='B'""",
            (wave_b_campaign_id,),
        )
        wave_b = cur.fetchone()
    f4_analysis = dict(f4["analysis"] or {}) if f4 else {}
    expected_configuration = champion_configuration(payload)
    f4_passed = bool(
        f4
        and f4["status"] == "ANALYZED"
        and f4["campaign_status"] == "STOPPED"
        and str(f4["manifest_sha256"]) == source["f4_manifest_sha256"]
        and str(f4["analysis_sha256"]) == source["f4_analysis_sha256"]
        and f4_analysis.get("decision") == "CONFIRMED_CHAMPION"
        and f4_analysis.get("selected_treatment") == "E"
        and f4_analysis.get("selected_primary_run_id")
        == payload["champion"]["source_primary_run_id"]
        and f4_analysis.get("selected_configuration") == expected_configuration
    )
    wave_b_passed = bool(
        wave_b
        and wave_b["status"] == "ANALYZED"
        and wave_b["campaign_status"] == "STOPPED"
        and str(wave_b["analysis_sha256"]) == source["wave_b_analysis_sha256"]
    )
    final_root = settings.artifact_dir / "primary-wave-b"
    final_analysis = _json_file_gate(
        final_root / "final-five-seed-analysis.json",
        str(source["final_five_seed_analysis_sha256"]),
    )
    final_index_path = final_root / "report-final-five-seed" / "final-five-seed-report-index.json"
    final_index_sha256 = _file_sha256(final_index_path) if final_index_path.is_file() else None
    try:
        final_index = (
            json.loads(final_index_path.read_text(encoding="utf-8"))
            if final_index_path.is_file()
            else {}
        )
    except (OSError, json.JSONDecodeError):
        final_index = {}
    final_report_payload_sha256 = str(final_index.get("payload_sha256") or "")
    final_report_passed = bool(
        final_index_sha256 == source["final_report_index_sha256"]
        and final_report_payload_sha256 == source["final_report_payload_sha256"]
    )
    f4_summary = (
        {
            key: f4[key]
            for key in (
                "f4_block_id",
                "status",
                "campaign_status",
                "manifest_sha256",
                "analysis_sha256",
            )
        }
        if f4
        else None
    )
    return {
        "passed": f4_passed and wave_b_passed and final_analysis["passed"] and final_report_passed,
        "f4": {"passed": f4_passed, "row": f4_summary},
        "wave_b": {"passed": wave_b_passed, "row": dict(wave_b) if wave_b else None},
        "final_analysis": final_analysis,
        "final_report": {
            "passed": final_report_passed,
            "path": str(final_index_path),
            "index_sha256": final_index_sha256,
            "payload_sha256": final_report_payload_sha256,
        },
    }


def _schema_and_deployment(settings: Settings) -> tuple[bool, dict[str, Any] | None]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('charm_control.experiment_v2_apply_best_deployments') "
            "IS NOT NULL AS installed"
        )
        installed = bool(cur.fetchone()["installed"])  # type: ignore[index]
        row = None
        if installed:
            cur.execute(
                """SELECT * FROM charm_control.experiment_v2_apply_best_deployments
                   ORDER BY created_at LIMIT 1"""
            )
            result = cur.fetchone()
            row = dict(result) if result else None
    return installed, row


def _health_snapshot(settings: Settings) -> dict[str, Any]:
    safety = _target_safety(settings, require_idle=False)
    healthy = bool(
        safety["pending_restart"] == 0
        and safety["managed_indexes"] == 0
        and safety["active_charm_sessions"] == 0
        and safety["active_campaigns"] == 0
    )
    return {**safety, "healthy": healthy}


def _active_configuration(settings: Settings, requested: Mapping[str, str]) -> dict[str, Any]:
    metadata = discover_knobs(settings, set(requested))
    active = {str(row["name"]): str(row["setting"]) for row in metadata}
    return {
        "active": active,
        "matches_requested": settings_equivalent(requested, active, metadata),
        "pending_restart": [str(row["name"]) for row in metadata if row["pending_restart"]],
    }


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def operator_time_estimate(
    apply_durations: Sequence[float],
    rollback_durations: Sequence[float],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    contract = dict(payload["time_estimate"])
    percentile = float(contract["historical_percentile"])
    apply_p95 = _nearest_rank(apply_durations, percentile)
    rollback_p95 = _nearest_rank(rollback_durations, percentile)
    activation = math.ceil(
        apply_p95 + float(contract["post_activation_verification_allowance_seconds"])
    )
    rollback = math.ceil(
        rollback_p95 + float(contract["post_rollback_verification_allowance_seconds"])
    )
    full_window = math.ceil(
        float(contract["readiness_and_decision_allowance_seconds"]) + activation + rollback
    )
    return {
        "basis": "exact-champion historical durations plus frozen operator allowances",
        "sample_count": len(apply_durations),
        "historical_percentile": percentile,
        "apply_duration_percentile_seconds": apply_p95,
        "rollback_duration_percentile_seconds": rollback_p95,
        "activation_operator_estimate_seconds": activation,
        "emergency_rollback_operator_estimate_seconds": rollback,
        "full_reversible_operator_window_seconds": full_window,
        "full_reversible_operator_window_minutes": full_window / 60.0,
    }


def _duration_history(
    settings: Settings, configuration: Mapping[str, str]
) -> tuple[list[float], list[float]]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT a.apply_duration_seconds,r.duration_seconds AS rollback_duration_seconds
               FROM charm_control.configuration_applications a
               LEFT JOIN charm_control.rollbacks r USING(application_id)
               WHERE a.requested_settings=%s AND a.apply_duration_seconds IS NOT NULL
               ORDER BY a.created_at""",
            (Jsonb(dict(configuration)),),
        )
        rows = cur.fetchall()
    applies = [float(row["apply_duration_seconds"]) for row in rows]
    rollbacks = [
        float(row["rollback_duration_seconds"])
        for row in rows
        if row["rollback_duration_seconds"] is not None
    ]
    return applies, rollbacks


def apply_best_readiness(
    settings: Settings, manifest_path: Path = APPLY_BEST_MANIFEST
) -> dict[str, Any]:
    manifest_sha256, payload = apply_best_manifest_payload(manifest_path)
    configuration = champion_configuration(payload)
    metadata = discover_knobs(settings, set(configuration))
    validate_candidate(configuration, metadata)
    installed, deployment = _schema_and_deployment(settings)
    source_gate = _source_gate(settings, payload) if installed else {"passed": False}
    target = _health_snapshot(settings)
    active = _active_configuration(settings, configuration)
    blockers: list[str] = []
    if not installed:
        blockers.append("migration-042-not-applied")
    if installed and not source_gate["passed"]:
        blockers.append("source-evidence-not-authenticated")
    if not target["healthy"]:
        blockers.append("target-is-not-clean-and-healthy")
    if deployment is not None and deployment["manifest_sha256"] != manifest_sha256:
        blockers.append("existing-deployment-has-different-manifest")
    applies, rollbacks = _duration_history(settings, configuration) if installed else ([], [])
    estimate = operator_time_estimate(applies, rollbacks, payload)
    status = str(deployment["status"]) if deployment else None
    return {
        "preparation_ready": not blockers,
        "recovery_test_ready": not blockers and status in {"PREPARED", "RECOVERY_TESTING"},
        "activation_ready": not blockers and status == "RECOVERY_TESTED",
        "activation_authorized": bool(
            deployment and deployment.get("authorization_recorded_at") is not None
        ),
        "persistent_activation_authorized_by_manifest": False,
        "blockers": blockers,
        "manifest_sha256": manifest_sha256,
        "configuration_sha256": _canonical_sha256(configuration),
        "configuration": configuration,
        "schema_installed": installed,
        "source_gate": source_gate,
        "target_health": target,
        "active_configuration": active,
        "deployment": deployment,
        "operator_time_estimate": estimate,
    }


def prepare_apply_best(settings: Settings, manifest_path: Path = APPLY_BEST_MANIFEST) -> uuid.UUID:
    readiness = apply_best_readiness(settings, manifest_path)
    if readiness["preparation_ready"] is not True:
        raise ValueError(f"apply-best preparation is blocked: {readiness['blockers']}")
    existing = readiness["deployment"]
    if existing is not None:
        return uuid.UUID(str(existing["deployment_id"]))
    _, payload = apply_best_manifest_payload(manifest_path)
    source = dict(payload["source_evidence"])
    configuration = champion_configuration(payload)
    deployment_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"charmdb:apply-best:{readiness['manifest_sha256']}"
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_apply_best_deployments
               (deployment_id,protocol_id,evidence_role,manifest_sha256,champion_treatment,
                source_primary_run_id,f4_campaign_id,f4_analysis_sha256,wave_b_campaign_id,
                wave_b_analysis_sha256,final_analysis_sha256,configuration_sha256,
                requested_configuration,status)
               VALUES (
                   %s,'thesis-protocol-v2','DEPLOYMENT_CONTROL',%s,'E',%s,%s,%s,%s,
                   %s,%s,%s,%s,'PREPARED'
               )""",
            (
                deployment_id,
                readiness["manifest_sha256"],
                payload["champion"]["source_primary_run_id"],
                source["f4_campaign_id"],
                source["f4_analysis_sha256"],
                source["wave_b_campaign_id"],
                source["wave_b_analysis_sha256"],
                source["final_five_seed_analysis_sha256"],
                readiness["configuration_sha256"],
                Jsonb(configuration),
            ),
        )
        conn.commit()
    return deployment_id


def _deployment(settings: Settings, deployment_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM charm_control.experiment_v2_apply_best_deployments "
            "WHERE deployment_id=%s",
            (deployment_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown apply-best deployment {deployment_id}")
    return dict(row)


def _mark_failed(
    settings: Settings,
    deployment_id: uuid.UUID,
    error: Exception,
    *,
    recovery_application_id: uuid.UUID | None = None,
    activation_application_id: uuid.UUID | None = None,
) -> None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_apply_best_deployments
               SET status='FAILED',failure_details=%s,
                   recovery_application_id=COALESCE(
                       recovery_application_id,
                       (SELECT application_id FROM charm_control.configuration_applications
                        WHERE application_id=%s)
                   ),
                   activation_application_id=COALESCE(
                       activation_application_id,
                       (SELECT application_id FROM charm_control.configuration_applications
                        WHERE application_id=%s)
                   )
               WHERE deployment_id=%s
                 AND status NOT IN ('FAILED','ROLLED_BACK')""",
            (
                Jsonb({"error": str(error)}),
                recovery_application_id,
                activation_application_id,
                deployment_id,
            ),
        )
        conn.commit()


def run_apply_best_recovery_test(settings: Settings, deployment_id: uuid.UUID) -> dict[str, Any]:
    row = _deployment(settings, deployment_id)
    if row["status"] not in {"PREPARED", "RECOVERY_TESTING", "FAILED"}:
        raise ValueError(
            "recovery test requires PREPARED, RECOVERY_TESTING, or recoverable FAILED state"
        )
    configuration = {str(k): str(v) for k, v in dict(row["requested_configuration"]).items()}
    application_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{deployment_id}:recovery")
    if row["status"] in {"PREPARED", "FAILED"}:
        if row["status"] == "FAILED":
            with connect(settings.control_dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT a.status,r.verified
                       FROM charm_control.configuration_applications a
                       JOIN charm_control.rollbacks r USING(application_id)
                       WHERE a.application_id=%s""",
                    (application_id,),
                )
                recovered = cur.fetchone()
            if not recovered or recovered["status"] != "ROLLED_BACK" or not recovered["verified"]:
                raise ValueError("failed recovery test has no verified durable rollback")
            if not _health_snapshot(settings)["healthy"]:
                raise ValueError("failed recovery test cannot resume until the target is healthy")
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_apply_best_deployments
                   SET status='RECOVERY_TESTING',recovery_application_id=%s,
                       recovery_started_at=clock_timestamp()
                   WHERE deployment_id=%s AND status=%s""",
                (application_id, deployment_id, row["status"]),
            )
            conn.commit()
    application = None
    try:
        application = apply_configuration(
            settings, configuration, application_id=application_id, force_restart=True
        )
        active = _active_configuration(settings, configuration)
        health_after_apply = _health_snapshot(settings)
        if not active["matches_requested"] or active["pending_restart"]:
            raise RuntimeError("recovery test could not verify the exact active champion settings")
        if not health_after_apply["healthy"]:
            raise RuntimeError("target health check failed after recovery-test activation")
        restored = rollback_configuration(
            settings, application_id, "D070 apply-best recovery integration test"
        )
        health_after_rollback = _health_snapshot(settings)
        if restored != application.previous:
            raise RuntimeError("recovery test did not restore the exact captured snapshot")
        if not health_after_rollback["healthy"]:
            raise RuntimeError("target health check failed after recovery-test rollback")
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT duration_seconds FROM charm_control.rollbacks WHERE application_id=%s",
                (application_id,),
            )
            rollback_row = cur.fetchone()
            if rollback_row is None:
                raise RuntimeError("recovery test has no durable rollback record")
            result = {
                "application_id": str(application_id),
                "snapshot_id": str(application.snapshot_id),
                "configuration_sha256": row["configuration_sha256"],
                "requested": application.requested,
                "verified": application.verified,
                "captured_previous": application.previous,
                "restored": restored,
                "apply_duration_seconds": application.duration_seconds,
                "rollback_duration_seconds": float(rollback_row["duration_seconds"]),
                "health_after_apply": health_after_apply,
                "health_after_rollback": health_after_rollback,
            }
            cur.execute(
                """UPDATE charm_control.experiment_v2_apply_best_deployments
                   SET status='RECOVERY_TESTED',recovery_application_id=%s,recovery_result=%s,
                       recovery_completed_at=clock_timestamp()
                   WHERE deployment_id=%s AND status='RECOVERY_TESTING'""",
                (application_id, Jsonb(result), deployment_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("apply-best deployment lost RECOVERY_TESTING state")
            conn.commit()
        return result
    except Exception as error:
        if application is not None:
            try:
                rollback_configuration(
                    settings, application_id, f"recovery-test cleanup after failure: {error}"
                )
            except Exception as rollback_error:
                error = RuntimeError(f"{error}; recovery-test cleanup failed: {rollback_error}")
        _mark_failed(settings, deployment_id, error, recovery_application_id=application_id)
        raise


def activate_apply_best(
    settings: Settings,
    deployment_id: uuid.UUID,
    *,
    decision_id: str,
    actor: str,
    statement: str,
    confirmed_configuration_sha256: str,
) -> dict[str, Any]:
    row = _deployment(settings, deployment_id)
    if row["status"] not in {"RECOVERY_TESTED", "ACTIVATING"}:
        raise ValueError("activation requires RECOVERY_TESTED or resumable ACTIVATING state")
    for label, value in (
        ("decision-id", decision_id),
        ("actor", actor),
        ("statement", statement),
    ):
        if not value.strip():
            raise ValueError(f"{label} cannot be empty")
    if confirmed_configuration_sha256 != row["configuration_sha256"]:
        raise ValueError("confirmed configuration SHA-256 does not match champion E")
    configuration = {str(k): str(v) for k, v in dict(row["requested_configuration"]).items()}
    application_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{deployment_id}:activation")
    if row["status"] == "RECOVERY_TESTED":
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_apply_best_deployments
                   SET status='ACTIVATING',authorization_decision_id=%s,authorization_actor=%s,
                       authorization_statement=%s,authorization_recorded_at=clock_timestamp()
                   WHERE deployment_id=%s AND status='RECOVERY_TESTED'""",
                (decision_id.strip(), actor.strip(), statement.strip(), deployment_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("apply-best deployment lost RECOVERY_TESTED state")
            conn.commit()
    else:
        if (
            row["authorization_decision_id"] != decision_id.strip()
            or row["authorization_actor"] != actor.strip()
            or row["authorization_statement"] != statement.strip()
        ):
            raise ValueError("resumed activation must use the persisted authorization verbatim")
    try:
        application = apply_configuration(
            settings, configuration, application_id=application_id, force_restart=True
        )
        active = _active_configuration(settings, configuration)
        health = _health_snapshot(settings)
        if not active["matches_requested"] or active["pending_restart"] or not health["healthy"]:
            raise RuntimeError("persistent activation failed exact-setting or target-health checks")
        result = {
            "application_id": str(application_id),
            "snapshot_id": str(application.snapshot_id),
            "configuration_sha256": row["configuration_sha256"],
            "requested": application.requested,
            "verified": application.verified,
            "captured_previous": application.previous,
            "apply_duration_seconds": application.duration_seconds,
            "health": health,
        }
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_apply_best_deployments
                   SET status='ACTIVE',activation_application_id=%s,activation_result=%s,
                       activated_at=clock_timestamp()
                   WHERE deployment_id=%s AND status='ACTIVATING'""",
                (application_id, Jsonb(result), deployment_id),
            )
            conn.commit()
        return result
    except Exception as error:
        _mark_failed(settings, deployment_id, error, activation_application_id=application_id)
        raise


def rollback_apply_best(
    settings: Settings, deployment_id: uuid.UUID, reason: str
) -> dict[str, Any]:
    row = _deployment(settings, deployment_id)
    if row["status"] not in {"ACTIVATING", "ACTIVE", "ROLLING_BACK", "FAILED"}:
        raise ValueError(
            "apply-best rollback requires ACTIVATING, ACTIVE, ROLLING_BACK, "
            "or an authorized FAILED state"
        )
    if not reason.strip():
        raise ValueError("rollback reason cannot be empty")
    if row["status"] == "FAILED" and row["authorization_recorded_at"] is None:
        raise ValueError("recovery-test failure cannot use the activation rollback path")
    application_id = (
        uuid.UUID(str(row["activation_application_id"]))
        if row["activation_application_id"] is not None
        else uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{deployment_id}:activation")
    )
    if row["status"] in {"ACTIVATING", "ACTIVE", "FAILED"}:
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_apply_best_deployments
                   SET status='ROLLING_BACK',activation_application_id=%s
                   WHERE deployment_id=%s AND status=%s""",
                (application_id, deployment_id, row["status"]),
            )
            conn.commit()
    try:
        restored = rollback_configuration(settings, application_id, reason.strip())
        health = _health_snapshot(settings)
        if not health["healthy"]:
            raise RuntimeError("target health check failed after apply-best rollback")
        result = {"application_id": str(application_id), "restored": restored, "health": health}
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_apply_best_deployments
                   SET status='ROLLED_BACK',activation_result=activation_result || %s,
                       rolled_back_at=clock_timestamp()
                   WHERE deployment_id=%s AND status='ROLLING_BACK'""",
                (Jsonb({"rollback": result}), deployment_id),
            )
            conn.commit()
        return result
    except Exception as error:
        _mark_failed(settings, deployment_id, error)
        raise


def apply_best_status(
    settings: Settings,
    deployment_id: uuid.UUID | None = None,
    manifest_path: Path = APPLY_BEST_MANIFEST,
) -> dict[str, Any]:
    readiness = apply_best_readiness(settings, manifest_path)
    if deployment_id is not None:
        row = _deployment(settings, deployment_id)
        readiness["deployment"] = row
    configuration = readiness["configuration"]
    active = _active_configuration(settings, configuration)
    row = readiness["deployment"]
    expected_active = bool(row and row["status"] == "ACTIVE")
    return {
        **readiness,
        "active_configuration": active,
        "expected_champion_active": expected_active,
        "state_matches_target": active["matches_requested"] == expected_active,
    }
