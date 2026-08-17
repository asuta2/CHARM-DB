from __future__ import annotations

import hashlib
import json
import subprocess
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.controller import discover_knobs, validate_candidate
from charmdb.db import connect
from charmdb.manifest_preflight import (
    _run_restore,
    _sha256,
    _target_safety,
    validate_dataset_restore,
)
from charmdb.v2.candidate_restore import (
    capture_live_fingerprint,
    compare_fingerprints,
    standardize_logical_restore_state,
)
from charmdb.v2.default_reference import (
    FROZEN_BASELINE_ID,
    FROZEN_CLIENT_THREADS,
    FROZEN_CONCURRENCY,
    FROZEN_MEASUREMENT_SECONDS,
    FROZEN_PREFLIGHT_ID,
    FROZEN_PROFILE_ID,
    FROZEN_RESTORE_MECHANISM,
    FROZEN_WARMUP_SECONDS,
)
from charmdb.v2.protocol import load_manifest
from charmdb.v2.screening import (
    PARAMETER_ORDER,
    SCREENING_MANIFEST,
    ScreeningStep,
    _analysis_points,
    _canonical_sha256,
    analyze_screening_observations,
    screening_initial_schedule,
    screening_manifest_payload,
    screening_oat_plan_rows,
)
from charmdb.worker import (
    control_campaign,
    create_campaign,
    create_v2_tuned_benchmark_trial,
    run_once,
)

RECOVERY_MANIFEST = Path("v2/config/parameter-screening-recovery.json")
REMEDIATION_MANIFEST = Path("v2/config/parameter-screening-recovery-remediation.json")
RECOVERY_STAGE = "parameter-screening-recovery"
REMEDIATION_STAGE = "parameter-screening-recovery-remediation"
SOURCE_CAMPAIGN_ID = uuid.UUID("df1196e0-3278-4be8-93d0-4bb05c98386f")
SOURCE_BLOCK_ID = uuid.UUID("30e32aba-c068-542f-adf7-cef29ea9c9d4")
SOURCE_MANIFEST_SHA256 = "bc7473666ca34eb81163166b16ea82b88a05a20b8e27250f9b13c5ef87a2b83a"
TARGET_IMAGE_DIGEST = "sha256:5773fe724c49c42a7a9ca70202e11e1dff21fb7235b335a73f39297d200b73a2"
RECOVERY_SCHEDULE_SHA256 = "811af62df14d250c7855706db36f6a500cee52ceb3a710d1fb1adaf455166b23"
D031_VALIDATION_BLOCK_ID = uuid.UUID("ba314a66-9d74-5dec-b50a-1b719f356ce4")
D031_RESULT_SHA256 = "6b64b8935557faff3da2490924aae361bafdd788273ceadf51734a2e7b61c0d9"


def _json_safe(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def recovery_manifest_payload(
    path: Path = RECOVERY_MANIFEST,
) -> tuple[str, dict[str, Any]]:
    manifest = load_manifest(path)
    if manifest.stage not in {RECOVERY_STAGE, REMEDIATION_STAGE} or manifest.evidence_role != (
        "CALIBRATION"
    ):
        raise ValueError("screening recovery requires its dedicated CALIBRATION manifest")
    payload = manifest.payload
    source = dict(payload["source"])
    expected_source = {
        "campaign_id": str(SOURCE_CAMPAIGN_ID),
        "block_id": str(SOURCE_BLOCK_ID),
        "manifest_sha256": SOURCE_MANIFEST_SHA256,
    }
    if any(source.get(key) != value for key, value in expected_source.items()):
        raise ValueError("screening-recovery source lineage differs from D030")
    mitigation = dict(payload["mitigation"])
    if (
        mitigation.get("mechanism") != "docker-compose-init-process"
        or mitigation.get("compose_service") != "target-postgres"
        or mitigation.get("compose_init") is not True
        or mitigation.get("target_image_digest") != TARGET_IMAGE_DIGEST
        or mitigation.get("target_postgresql_version") != "18.1"
        or mitigation.get("image_or_major_minor_change_permitted") is not False
        or mitigation.get("restore_parameter_change_permitted") is not False
    ):
        raise ValueError("screening-recovery mitigation differs from D031")
    if manifest.stage == REMEDIATION_STAGE:
        supersedes = dict(payload.get("supersedes") or {})
        remediation = dict(payload.get("remediation") or {})
        if (
            supersedes.get("validation_block_id") != str(D031_VALIDATION_BLOCK_ID)
            or supersedes.get("manifest_sha256")
            != "55e69fb49db2f9374fb6d48544f8e7224fea134708b1505cc3da7a5feb749eb0"
            or supersedes.get("required_status") != "FAILED"
            or supersedes.get("result_sha256") != D031_RESULT_SHA256
            or remediation.get("method") != "drop-and-recreate-synthetic-target-database"
            or remediation.get("target_database") != "charm_target"
            or remediation.get("expected_orphan_filenodes") != [291710, 291725]
            or remediation.get("expected_orphan_bytes") != 7_777_845_248
            or remediation.get("manual_file_deletion_permitted") is not False
        ):
            raise ValueError("screening-recovery remediation differs from D032")
    _, screening_payload = screening_manifest_payload(SCREENING_MANIFEST)
    expected_schedule = [
        item
        for item in screening_initial_schedule(screening_payload)
        if item["position"] in (34, 35)
    ]
    recovery = dict(payload["recovery_schedule"])
    if recovery.get("observations") != expected_schedule:
        raise ValueError("recovery observations differ from source positions 34 and 35")
    if (
        recovery.get("schedule_sha256") != RECOVERY_SCHEDULE_SHA256
        or _canonical_sha256(expected_schedule) != RECOVERY_SCHEDULE_SHA256
    ):
        raise ValueError("screening-recovery schedule hash does not verify")
    return hashlib.sha256(path.read_bytes()).hexdigest(), payload


def recovery_design_summary(path: Path = RECOVERY_MANIFEST) -> dict[str, Any]:
    manifest_sha256, payload = recovery_manifest_payload(path)
    return {
        "status": payload["status"],
        "execution_ready": payload["execution_ready"],
        "manifest_sha256": manifest_sha256,
        "source_campaign_id": payload["source"]["campaign_id"],
        "source_block_id": payload["source"]["block_id"],
        "mitigation_sha256": _canonical_sha256(payload["mitigation"]),
        "restore_validation_repetitions": payload["restore_stability_validation"][
            "consecutive_repetitions"
        ],
        "recovery_schedule_sha256": payload["recovery_schedule"]["schedule_sha256"],
        "recovery_observations": len(payload["recovery_schedule"]["observations"]),
        "maximum_oat_observations": payload["runtime"]["maximum_oat_observations"],
        "unresolved_decisions": payload["unresolved_decisions"],
    }


def _container_init_evidence() -> dict[str, Any]:
    container = subprocess.run(
        ["docker", "compose", "ps", "-q", "target-postgres"],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    container_ids = container.stdout.strip().splitlines()
    if container.returncode != 0 or not container_ids:
        raise RuntimeError("target container is unavailable for init-process verification")
    container_id = container_ids[0]
    inspected = subprocess.run(
        [
            "docker",
            "inspect",
            container_id,
            "--format",
            "{{json .HostConfig.Init}}|{{.Image}}|{{.Config.Image}}",
        ],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if inspected.returncode != 0 or not inspected.stdout.strip():
        raise RuntimeError("target container init configuration could not be inspected")
    fields = inspected.stdout.strip().split("|", 2)
    if len(fields) != 3:
        raise RuntimeError("target container init evidence is malformed")
    pid_one = subprocess.run(
        ["docker", "exec", container_id, "sh", "-c", "ps -o comm= -p 1"],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if pid_one.returncode != 0 or not pid_one.stdout.strip():
        raise RuntimeError("target container PID 1 could not be inspected")
    return {
        "container_id": container_id,
        "compose_init": fields[0] == "true",
        "image_digest": fields[1],
        "configured_image": fields[2],
        "pid_one_command": pid_one.stdout.strip(),
        "postgres_is_pid_one": pid_one.stdout.strip() == "postgres",
    }


def _source_state(settings: Settings) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT b.*,c.status AS campaign_status
               FROM charm_control.experiment_v2_screening_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.block_id=%s AND b.campaign_id=%s""",
            (SOURCE_BLOCK_ID, SOURCE_CAMPAIGN_ID),
        )
        block = cur.fetchone()
        cur.execute(
            """SELECT status,count(*) AS count
               FROM charm_control.experiment_v2_screening_runs
               WHERE block_id=%s GROUP BY status""",
            (SOURCE_BLOCK_ID,),
        )
        counts = {str(row["status"]): int(row["count"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT run_id,sequence,evaluation_kind,sobol_index,random_seed,
                      requested_configuration,status
               FROM charm_control.experiment_v2_screening_runs
               WHERE block_id=%s AND sequence IN (34,35) ORDER BY sequence""",
            (SOURCE_BLOCK_ID,),
        )
        source_runs = [dict(row) for row in cur.fetchall()]
    if block is None:
        raise ValueError("D031 requires the retained D030 source block")
    return {"block": dict(block), "counts": counts, "source_runs": source_runs}


def _validate_source_state(source: dict[str, Any], payload: dict[str, Any]) -> None:
    block = source["block"]
    if (
        block["status"] != "FAILED"
        or block["campaign_status"] != "STOPPED"
        or block["manifest_sha256"] != SOURCE_MANIFEST_SHA256
        or block["initial_analysis_sha256"] is not None
        or block["final_analysis_sha256"] is not None
    ):
        raise ValueError("D030 source block is not the frozen terminal failure")
    if source["counts"] != {"COMPLETED": 33, "FAILED": 1, "PLANNED": 1}:
        raise ValueError("D030 source run counts differ from D031 pre-registration")
    expected = list(payload["recovery_schedule"]["observations"])
    observed = source["source_runs"]
    if len(observed) != 2:
        raise ValueError("D030 source positions 34 and 35 are missing")
    for row, item, status in zip(observed, expected, ("FAILED", "PLANNED"), strict=True):
        if (
            int(row["sequence"]) != int(item["position"])
            or row["evaluation_kind"] != item["kind"]
            or row["sobol_index"] != item.get("sobol_index")
            or int(row["random_seed"]) != int(payload["recovery_schedule"]["random_seed"])
            or dict(row["requested_configuration"]) != dict(item["configuration"])
            or row["status"] != status
        ):
            raise ValueError("D030 source recovery slots differ from the frozen schedule")


def restore_stability_readiness(
    settings: Settings,
    manifest_path: Path = RECOVERY_MANIFEST,
) -> dict[str, Any]:
    manifest_sha256, payload = recovery_manifest_payload(manifest_path)
    source = _source_state(settings)
    _validate_source_state(source, payload)
    init_evidence = _container_init_evidence()
    target = _target_safety(settings)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT validation_block_id,status,result_sha256
               FROM charm_control.experiment_v2_restore_stability_blocks
               WHERE source_screening_block_id=%s AND manifest_sha256=%s""",
            (SOURCE_BLOCK_ID, manifest_sha256),
        )
        existing = cur.fetchone()
        remediation = None
        if payload["stage"] == REMEDIATION_STAGE:
            cur.execute(
                """SELECT remediation_id,status,result_sha256
                   FROM charm_control.experiment_v2_target_database_remediations
                   WHERE manifest_sha256=%s""",
                (manifest_sha256,),
            )
            remediation = cur.fetchone()
    mitigation_sha256 = _canonical_sha256(payload["mitigation"])
    init_passed = (
        init_evidence["compose_init"] is True
        and init_evidence["postgres_is_pid_one"] is False
        and init_evidence["image_digest"] == TARGET_IMAGE_DIGEST
    )
    return {
        "ready": (
            payload["status"] == "ready"
            and payload["execution_ready"] is True
            and init_passed
            and existing is None
            and (
                payload["stage"] == RECOVERY_STAGE
                or (
                    remediation is not None
                    and remediation["status"] == "PASSED"
                    and isinstance(remediation["result_sha256"], str)
                )
            )
            and target["active_campaigns"] == 0
        ),
        "manifest_sha256": manifest_sha256,
        "mitigation_sha256": mitigation_sha256,
        "source_block_id": str(SOURCE_BLOCK_ID),
        "source_status": source["block"]["status"],
        "source_campaign_status": source["block"]["campaign_status"],
        "container_init": init_evidence,
        "target_safety": target,
        "remediation": dict(remediation) if remediation is not None else None,
        "existing_validation": dict(existing) if existing is not None else None,
    }


def create_restore_stability_plan(
    settings: Settings,
    manifest_path: Path = RECOVERY_MANIFEST,
) -> uuid.UUID:
    readiness = restore_stability_readiness(settings, manifest_path)
    if readiness["ready"] is not True:
        raise ValueError("restore-stability validation is blocked until every gate passes")
    validation_block_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"charmdb:v2:screening-recovery:restore:{SOURCE_BLOCK_ID}:{readiness['manifest_sha256']}",
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        remediation_id = (
            readiness["remediation"]["remediation_id"]
            if readiness["remediation"] is not None
            else None
        )
        supersedes = D031_VALIDATION_BLOCK_ID if readiness["remediation"] is not None else None
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_restore_stability_blocks
               (validation_block_id,source_screening_block_id,protocol_id,evidence_role,
                manifest_sha256,mitigation_sha256,status,required_repetitions,
                remediation_id,supersedes_validation_block_id)
               VALUES (%s,%s,'thesis-protocol-v2','INFRASTRUCTURE',%s,%s,'PLANNED',3,%s,%s)""",
            (
                validation_block_id,
                SOURCE_BLOCK_ID,
                readiness["manifest_sha256"],
                readiness["mitigation_sha256"],
                remediation_id,
                supersedes,
            ),
        )
        for sequence in range(1, 4):
            run_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"charmdb:{validation_block_id}:restore-stability:{sequence}",
            )
            cur.execute(
                """INSERT INTO charm_control.experiment_v2_restore_stability_runs
                   (validation_run_id,validation_block_id,sequence,status)
                   VALUES (%s,%s,%s,'PLANNED')""",
                (run_id, validation_block_id, sequence),
            )
        conn.commit()
    return validation_block_id


def restore_stability_history(settings: Settings, validation_block_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_v2_restore_stability_blocks
               WHERE validation_block_id=%s""",
            (validation_block_id,),
        )
        block = cur.fetchone()
        cur.execute(
            """SELECT * FROM charm_control.experiment_v2_restore_stability_runs
               WHERE validation_block_id=%s ORDER BY sequence""",
            (validation_block_id,),
        )
        runs = [dict(row) for row in cur.fetchall()]
    if block is None:
        raise ValueError(f"unknown restore-stability block {validation_block_id}")
    return {"block": dict(block), "runs": runs}


def _baseline(settings: Settings) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT exact_core,physical_statistics,physical_tolerances
               FROM charm_control.experiment_candidate_dataset_baselines
               WHERE baseline_id=%s AND approved""",
            (FROZEN_BASELINE_ID,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError("D031 requires the approved frozen candidate baseline")
    return dict(row)


def _database_file_evidence(
    settings: Settings, registered_orphans: tuple[int, ...] = (291710, 291725)
) -> dict[str, Any]:
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT oid,pg_database_size(oid)::bigint AS database_size_bytes
               FROM pg_database WHERE datname=current_database()"""
        )
        identity = dict(cur.fetchone() or {})
        database_oid = int(identity["oid"])
        cur.execute(
            """SELECT pg_relation_filenode('public.pgbench_accounts'::regclass) AS heap,
                      pg_relation_filenode('public.pgbench_accounts_pkey'::regclass) AS index"""
        )
        current = dict(cur.fetchone() or {})
        relative_directory = f"base/{database_oid}"
        cur.execute("SELECT pg_ls_dir(%s) AS name", (relative_directory,))
        names = [str(row["name"]) for row in cur.fetchall()]
        orphan_groups: dict[str, dict[str, Any]] = {}
        for filenode in registered_orphans:
            matched = [
                name for name in names if name == str(filenode) or name.startswith(f"{filenode}.")
            ]
            total = 0
            for name in matched:
                cur.execute(
                    "SELECT (pg_stat_file(%s)).size::bigint AS bytes",
                    (f"{relative_directory}/{name}",),
                )
                total += int(cur.fetchone()["bytes"])  # type: ignore[index]
            orphan_groups[str(filenode)] = {
                "files": sorted(matched),
                "file_count": len(matched),
                "bytes": total,
            }
    return {
        "database_oid": database_oid,
        "database_size_bytes": int(identity["database_size_bytes"]),
        "current_heap_filenode": int(current["heap"]),
        "current_index_filenode": int(current["index"]),
        "registered_orphans": orphan_groups,
        "registered_orphan_bytes": sum(int(group["bytes"]) for group in orphan_groups.values()),
    }


def _failed_d031_validation(settings: Settings) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT validation_block_id,status,result_sha256
               FROM charm_control.experiment_v2_restore_stability_blocks
               WHERE validation_block_id=%s""",
            (D031_VALIDATION_BLOCK_ID,),
        )
        row = cur.fetchone()
    if row is None or row["status"] != "FAILED" or row["result_sha256"] != D031_RESULT_SHA256:
        raise ValueError("D032 requires the exact terminal D031 validation failure")
    return dict(row)


def target_database_remediation_readiness(
    settings: Settings,
    manifest_path: Path = REMEDIATION_MANIFEST,
) -> dict[str, Any]:
    manifest_sha256, payload = recovery_manifest_payload(manifest_path)
    if payload["stage"] != REMEDIATION_STAGE:
        raise ValueError("target-database remediation requires the D032 manifest")
    _validate_source_state(_source_state(settings), payload)
    failed = _failed_d031_validation(settings)
    init_evidence = _container_init_evidence()
    target = _target_safety(settings)
    remediation = dict(payload["remediation"])
    file_evidence = _database_file_evidence(
        settings, tuple(int(item) for item in remediation["expected_orphan_filenodes"])
    )
    expected_pre = (
        file_evidence["database_size_bytes"] == int(remediation["expected_pre_database_size_bytes"])
        and file_evidence["registered_orphan_bytes"] == int(remediation["expected_orphan_bytes"])
        and file_evidence["current_heap_filenode"] == int(remediation["current_heap_filenode"])
        and file_evidence["current_index_filenode"] == int(remediation["current_index_filenode"])
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT remediation_id,status,result_sha256
               FROM charm_control.experiment_v2_target_database_remediations
               WHERE manifest_sha256=%s""",
            (manifest_sha256,),
        )
        existing = cur.fetchone()
    return {
        "ready": (
            payload["status"] == "ready"
            and payload["execution_ready"] is True
            and failed["status"] == "FAILED"
            and init_evidence["compose_init"] is True
            and init_evidence["postgres_is_pid_one"] is False
            and init_evidence["image_digest"] == TARGET_IMAGE_DIGEST
            and target["active_campaigns"] == 0
            and expected_pre
            and existing is None
        ),
        "manifest_sha256": manifest_sha256,
        "remediation_sha256": _canonical_sha256(remediation),
        "failed_validation": failed,
        "container_init": init_evidence,
        "target_safety": target,
        "pre_evidence": file_evidence,
        "pre_evidence_matches_manifest": expected_pre,
        "existing_remediation": dict(existing) if existing is not None else None,
    }


def create_target_database_remediation(
    settings: Settings,
    manifest_path: Path = REMEDIATION_MANIFEST,
) -> uuid.UUID:
    readiness = target_database_remediation_readiness(settings, manifest_path)
    if readiness["ready"] is not True:
        raise ValueError("target-database remediation is blocked until every gate passes")
    remediation_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"charmdb:v2:screening-recovery:remediation:{readiness['manifest_sha256']}",
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_target_database_remediations
               (remediation_id,source_screening_block_id,failed_validation_block_id,
                protocol_id,evidence_role,manifest_sha256,remediation_sha256,status,
                pre_evidence)
               VALUES (%s,%s,%s,'thesis-protocol-v2','INFRASTRUCTURE',%s,%s,'PLANNED',%s)""",
            (
                remediation_id,
                SOURCE_BLOCK_ID,
                D031_VALIDATION_BLOCK_ID,
                readiness["manifest_sha256"],
                readiness["remediation_sha256"],
                Jsonb(readiness["pre_evidence"]),
            ),
        )
        conn.commit()
    return remediation_id


def target_database_remediation_history(
    settings: Settings, remediation_id: uuid.UUID
) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_v2_target_database_remediations
               WHERE remediation_id=%s""",
            (remediation_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown target-database remediation {remediation_id}")
    return dict(row)


def _snapshot_path(settings: Settings) -> Path:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT dataset_snapshot_sha256,snapshot_relative_path,snapshot_byte_size
               FROM charm_control.experiment_manifest_preflights WHERE preflight_id=%s""",
            (FROZEN_PREFLIGHT_ID,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError("D032 requires the frozen manifest preflight")
    artifact_root = settings.artifact_dir.resolve()
    snapshot = (settings.artifact_dir / str(row["snapshot_relative_path"])).resolve()
    try:
        snapshot.relative_to(artifact_root)
    except ValueError as exc:
        raise ValueError("dataset snapshot path escapes the artifact directory") from exc
    if (
        not snapshot.is_file()
        or snapshot.stat().st_size != int(row["snapshot_byte_size"])
        or _sha256(snapshot) != str(row["dataset_snapshot_sha256"])
    ):
        raise ValueError("D032 snapshot artifact differs from its persisted identity")
    return snapshot


def _recreate_target_database(settings: Settings) -> None:
    maintenance_dsn = make_conninfo(settings.target_dsn, dbname="postgres")
    with psycopg.connect(maintenance_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT pg_terminate_backend(pid) FROM pg_stat_activity
               WHERE datname=%s AND pid<>pg_backend_pid()""",
            (settings.target_db,),
        )
        cur.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(settings.target_db))
        )
        cur.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(settings.target_db), sql.Identifier(settings.target_user)
            )
        )


def run_target_database_remediation(
    settings: Settings,
    remediation_id: uuid.UUID,
    manifest_path: Path = REMEDIATION_MANIFEST,
) -> dict[str, Any]:
    manifest_sha256, payload = recovery_manifest_payload(manifest_path)
    history = target_database_remediation_history(settings, remediation_id)
    if history["manifest_sha256"] != manifest_sha256:
        raise ValueError("target-database remediation manifest differs from its ledger")
    if history["status"] in {"PASSED", "FAILED"}:
        return {
            "action": "already-terminal",
            "remediation_id": str(remediation_id),
            "status": history["status"],
        }
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_target_database_remediations
               SET status='RUNNING',started_at=clock_timestamp()
               WHERE remediation_id=%s AND status='PLANNED'""",
            (remediation_id,),
        )
        if cur.rowcount != 1:
            raise RuntimeError("target-database remediation could not be claimed")
        conn.commit()
    started = time.monotonic()
    try:
        _validate_source_state(_source_state(settings), payload)
        _failed_d031_validation(settings)
        if _database_file_evidence(settings) != history["pre_evidence"]:
            raise RuntimeError("target file evidence changed after D032 registration")
        snapshot = _snapshot_path(settings)
        _recreate_target_database(settings)
        _run_restore(settings, snapshot)
        standardize_logical_restore_state(settings)
        validation = validate_dataset_restore(settings, FROZEN_PREFLIGHT_ID)
        standardize_logical_restore_state(settings)
        exact_core, physical = capture_live_fingerprint(
            settings, FROZEN_PREFLIGHT_ID, validation.validation_id
        )
        baseline = _baseline(settings)
        comparison = compare_fingerprints(
            dict(baseline["exact_core"]),
            dict(baseline["physical_statistics"]),
            exact_core,
            physical,
            dict(baseline["physical_tolerances"]),
        )
        post_evidence = _database_file_evidence(settings)
        orphans_absent = post_evidence["registered_orphan_bytes"] == 0
        if not comparison.passed or not orphans_absent:
            raise RuntimeError("rebuilt target failed the frozen fingerprint or orphan gate")
        result = {
            "remediation_id": str(remediation_id),
            "outcome": "PASSED",
            "method": payload["remediation"]["method"],
            "validation_id": str(validation.validation_id),
            "exact_core_passed": comparison.exact_core_passed,
            "physical_statistics_passed": comparison.physical_statistics_passed,
            "registered_orphans_absent": orphans_absent,
            "duration_seconds": time.monotonic() - started,
        }
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_target_database_remediations
                   SET status='PASSED',post_evidence=%s,result=%s,result_sha256=%s,
                       completed_at=clock_timestamp()
                   WHERE remediation_id=%s AND status='RUNNING'""",
                (
                    Jsonb(post_evidence),
                    Jsonb(result),
                    _canonical_sha256(result),
                    remediation_id,
                ),
            )
            conn.commit()
        return result
    except Exception as error:
        result = {
            "remediation_id": str(remediation_id),
            "outcome": "FAILED",
            "error_type": type(error).__name__,
            "message": str(error),
            "duration_seconds": time.monotonic() - started,
        }
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_target_database_remediations
                   SET status='FAILED',result=%s,result_sha256=%s,
                       completed_at=clock_timestamp()
                   WHERE remediation_id=%s AND status='RUNNING'""",
                (Jsonb(result), _canonical_sha256(result), remediation_id),
            )
            conn.commit()
        raise


def run_restore_stability_next(
    settings: Settings,
    validation_block_id: uuid.UUID,
    manifest_path: Path = RECOVERY_MANIFEST,
) -> dict[str, Any]:
    manifest_sha256, payload = recovery_manifest_payload(manifest_path)
    history = restore_stability_history(settings, validation_block_id)
    block = history["block"]
    if block["manifest_sha256"] != manifest_sha256:
        raise ValueError("restore-stability manifest differs from the frozen block")
    if block["status"] in {"PASSED", "FAILED"}:
        return {
            "action": "already-terminal",
            "validation_block_id": str(validation_block_id),
            "status": block["status"],
        }
    _validate_source_state(_source_state(settings), payload)
    init_before = _container_init_evidence()
    if (
        init_before["compose_init"] is not True
        or init_before["postgres_is_pid_one"] is not False
        or init_before["image_digest"] != TARGET_IMAGE_DIGEST
    ):
        raise ValueError("D031 init-process mitigation is not active")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        if block["status"] == "PLANNED":
            cur.execute(
                """UPDATE charm_control.experiment_v2_restore_stability_blocks
                   SET status='RUNNING' WHERE validation_block_id=%s AND status='PLANNED'""",
                (validation_block_id,),
            )
        cur.execute(
            """SELECT * FROM charm_control.experiment_v2_restore_stability_runs
               WHERE validation_block_id=%s AND status='PLANNED'
               ORDER BY sequence LIMIT 1 FOR UPDATE""",
            (validation_block_id,),
        )
        planned = cur.fetchone()
        if planned is not None:
            cur.execute(
                """UPDATE charm_control.experiment_v2_restore_stability_runs
                   SET status='RUNNING',started_at=clock_timestamp()
                   WHERE validation_run_id=%s AND status='PLANNED'""",
                (planned["validation_run_id"],),
            )
        conn.commit()
    if planned is None:
        raise RuntimeError("running restore-stability block has no planned repetition")
    run_id = uuid.UUID(str(planned["validation_run_id"]))
    started = time.monotonic()
    try:
        validation = validate_dataset_restore(settings, FROZEN_PREFLIGHT_ID)
        standardize_logical_restore_state(settings)
        exact_core, physical = capture_live_fingerprint(
            settings, FROZEN_PREFLIGHT_ID, validation.validation_id
        )
        baseline = _baseline(settings)
        comparison = compare_fingerprints(
            dict(baseline["exact_core"]),
            dict(baseline["physical_statistics"]),
            exact_core,
            physical,
            dict(baseline["physical_tolerances"]),
        )
        init_after = _container_init_evidence()
        if not comparison.passed:
            raise RuntimeError("restore-stability repetition failed the frozen fingerprints")
        if (
            init_after["compose_init"] is not True
            or init_after["postgres_is_pid_one"] is not False
            or init_after["image_digest"] != TARGET_IMAGE_DIGEST
        ):
            raise RuntimeError("init-process mitigation changed during restore validation")
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
                """UPDATE charm_control.experiment_v2_restore_stability_runs
                   SET status='PASSED',restore_validation_id=%s,duration_seconds=%s,
                       verification_details=%s,completed_at=clock_timestamp()
                   WHERE validation_run_id=%s AND status='RUNNING'""",
                (validation.validation_id, duration, Jsonb(verification), run_id),
            )
            cur.execute(
                """SELECT validation_run_id,sequence,restore_validation_id,
                          duration_seconds,verification_details
                   FROM charm_control.experiment_v2_restore_stability_runs
                   WHERE validation_block_id=%s AND status='PASSED' ORDER BY sequence""",
                (validation_block_id,),
            )
            passed = [_json_safe(dict(row)) for row in cur.fetchall()]
            if len(passed) == 3:
                result = {
                    "validation_block_id": str(validation_block_id),
                    "source_screening_block_id": str(SOURCE_BLOCK_ID),
                    "manifest_sha256": manifest_sha256,
                    "mitigation_sha256": block["mitigation_sha256"],
                    "outcome": "PASSED",
                    "repetitions": passed,
                }
                result_sha256 = _canonical_sha256(result)
                cur.execute(
                    """UPDATE charm_control.experiment_v2_restore_stability_blocks
                       SET status='PASSED',result=%s,result_sha256=%s,
                           completed_at=clock_timestamp()
                       WHERE validation_block_id=%s AND status='RUNNING'""",
                    (Jsonb(result), result_sha256, validation_block_id),
                )
            conn.commit()
        return {
            "action": "validation-complete" if len(passed) == 3 else "repetition-passed",
            "validation_block_id": str(validation_block_id),
            "validation_run_id": str(run_id),
            "sequence": int(planned["sequence"]),
            "duration_seconds": duration,
            "completed_repetitions": len(passed),
            "status": "PASSED" if len(passed) == 3 else "RUNNING",
        }
    except Exception as error:
        duration = time.monotonic() - started
        failure = {"error_type": type(error).__name__, "message": str(error)}
        result = {
            "validation_block_id": str(validation_block_id),
            "source_screening_block_id": str(SOURCE_BLOCK_ID),
            "manifest_sha256": manifest_sha256,
            "mitigation_sha256": block["mitigation_sha256"],
            "outcome": "FAILED",
            "failed_sequence": int(planned["sequence"]),
            "failure": failure,
        }
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_restore_stability_runs
                   SET status='FAILED',duration_seconds=%s,failure_details=%s,
                       completed_at=clock_timestamp()
                   WHERE validation_run_id=%s AND status='RUNNING'""",
                (duration, Jsonb(failure), run_id),
            )
            cur.execute(
                """UPDATE charm_control.experiment_v2_restore_stability_blocks
                   SET status='FAILED',result=%s,result_sha256=%s,
                       completed_at=clock_timestamp()
                   WHERE validation_block_id=%s AND status='RUNNING'""",
                (Jsonb(result), _canonical_sha256(result), validation_block_id),
            )
            conn.commit()
        raise


def recovery_plan_rows(
    payload: dict[str, Any],
    recovery_block_id: uuid.UUID,
    campaign_id: uuid.UUID,
    source_runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    observations = list(payload["recovery_schedule"]["observations"])
    if len(source_runs) != 2:
        raise ValueError("screening recovery requires exactly two source runs")
    rows: list[dict[str, Any]] = []
    for observation, source in zip(observations, source_runs, strict=True):
        sequence = int(observation["position"])
        kind = str(observation["kind"])
        rows.append(
            {
                "recovery_run_id": uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"charmdb:{recovery_block_id}:source:{source['run_id']}:{sequence}",
                ),
                "recovery_block_id": recovery_block_id,
                "campaign_id": campaign_id,
                "source_run_id": source["run_id"],
                "sequence": sequence,
                "chronological_execution_index": sequence,
                "phase": "INITIAL",
                "evaluation_kind": kind,
                "sobol_index": observation.get("sobol_index"),
                "oat_parameter": None,
                "random_seed": int(payload["recovery_schedule"]["random_seed"]),
                "requested_configuration": observation["configuration"],
                "status": "PLANNED",
            }
        )
    return rows


def screening_recovery_readiness(
    settings: Settings,
    manifest_path: Path = RECOVERY_MANIFEST,
) -> dict[str, Any]:
    manifest_sha256, payload = recovery_manifest_payload(manifest_path)
    source = _source_state(settings)
    _validate_source_state(source, payload)
    init_evidence = _container_init_evidence()
    target = _target_safety(settings)
    metadata = discover_knobs(settings, set(PARAMETER_ORDER))
    for observation in payload["recovery_schedule"]["observations"]:
        validate_candidate(
            {str(name): str(value) for name, value in observation["configuration"].items()},
            metadata,
        )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT validation_block_id,status,result_sha256
               FROM charm_control.experiment_v2_restore_stability_blocks
               WHERE source_screening_block_id=%s AND manifest_sha256=%s""",
            (SOURCE_BLOCK_ID, manifest_sha256),
        )
        validation = cur.fetchone()
        cur.execute(
            """SELECT recovery_block_id,status,campaign_id
               FROM charm_control.experiment_v2_screening_recovery_blocks
               WHERE source_screening_block_id=%s""",
            (SOURCE_BLOCK_ID,),
        )
        existing = cur.fetchone()
    validation_passed = (
        validation is not None
        and validation["status"] == "PASSED"
        and isinstance(validation["result_sha256"], str)
    )
    init_passed = (
        init_evidence["compose_init"] is True
        and init_evidence["postgres_is_pid_one"] is False
        and init_evidence["image_digest"] == TARGET_IMAGE_DIGEST
    )
    return {
        "ready": (
            payload["status"] == "ready"
            and payload["execution_ready"] is True
            and validation_passed
            and init_passed
            and existing is None
            and target["active_campaigns"] == 0
        ),
        "manifest_sha256": manifest_sha256,
        "source_block_id": str(SOURCE_BLOCK_ID),
        "restore_validation": dict(validation) if validation is not None else None,
        "container_init": init_evidence,
        "target_safety": target,
        "existing_recovery": dict(existing) if existing is not None else None,
        "recovery_observations": 2,
        "maximum_oat_observations": 6,
    }


def create_screening_recovery_plan(
    settings: Settings,
    manifest_path: Path = RECOVERY_MANIFEST,
) -> uuid.UUID:
    readiness = screening_recovery_readiness(settings, manifest_path)
    if readiness["ready"] is not True:
        raise ValueError("screening recovery is blocked until every readiness gate passes")
    _, payload = recovery_manifest_payload(manifest_path)
    source = _source_state(settings)
    campaign_id = create_campaign(
        settings,
        "v2-parameter-screening-recovery",
        "CALIBRATION",
        {"maximize": "throughput_tps", "minimize": "p99_ms"},
        {"failures": 0, "p99_slo_applied": False},
        failure_limit=8,
        campaign_settings={
            "protocol_id": "thesis-protocol-v2",
            "stage": payload["stage"],
            "manifest_sha256": readiness["manifest_sha256"],
            "source_campaign_id": str(SOURCE_CAMPAIGN_ID),
            "source_block_id": str(SOURCE_BLOCK_ID),
            "restore_validation_block_id": str(
                readiness["restore_validation"]["validation_block_id"]
            ),
            "preflight_id": str(FROZEN_PREFLIGHT_ID),
            "candidate_restore_baseline_id": str(FROZEN_BASELINE_ID),
            "restore_mechanism": FROZEN_RESTORE_MECHANISM,
            "benchmark_profile_id": FROZEN_PROFILE_ID,
            "recovery_schedule_sha256": RECOVERY_SCHEDULE_SHA256,
            "manifest": payload,
        },
        actor="v2-screening-recovery",
    )
    recovery_block_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"charmdb:{campaign_id}:v2-screening-recovery"
    )
    rows = recovery_plan_rows(payload, recovery_block_id, campaign_id, source["source_runs"])
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_screening_recovery_blocks
               (recovery_block_id,campaign_id,source_screening_block_id,
                restore_validation_block_id,protocol_id,evidence_role,manifest_sha256,
                source_manifest_sha256,preflight_id,baseline_id,benchmark_profile_id,
                status,recovery_schedule_sha256,analysis_plan)
               VALUES (%s,%s,%s,%s,'thesis-protocol-v2','CALIBRATION',%s,%s,%s,%s,%s,
                       'RECOVERY_PLANNED',%s,%s)""",
            (
                recovery_block_id,
                campaign_id,
                SOURCE_BLOCK_ID,
                readiness["restore_validation"]["validation_block_id"],
                readiness["manifest_sha256"],
                SOURCE_MANIFEST_SHA256,
                FROZEN_PREFLIGHT_ID,
                FROZEN_BASELINE_ID,
                FROZEN_PROFILE_ID,
                RECOVERY_SCHEDULE_SHA256,
                Jsonb(screening_manifest_payload()[1]["analysis_plan"]),
            ),
        )
        for row in rows:
            cur.execute(
                """INSERT INTO charm_control.experiment_v2_screening_recovery_runs
                   (recovery_run_id,recovery_block_id,campaign_id,source_run_id,sequence,
                    chronological_execution_index,phase,evaluation_kind,sobol_index,
                    random_seed,requested_configuration,status)
                   VALUES (%s,%s,%s,%s,%s,%s,'INITIAL',%s,%s,%s,%s,'PLANNED')""",
                (
                    row["recovery_run_id"],
                    recovery_block_id,
                    campaign_id,
                    row["source_run_id"],
                    row["sequence"],
                    row["chronological_execution_index"],
                    row["evaluation_kind"],
                    row["sobol_index"],
                    row["random_seed"],
                    Jsonb(row["requested_configuration"]),
                ),
            )
        conn.commit()
    return campaign_id


def _recovery_block(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT b.*,c.status AS campaign_status
               FROM charm_control.experiment_v2_screening_recovery_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.campaign_id=%s""",
            (campaign_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown v2 screening-recovery campaign {campaign_id}")
    return dict(row)


def _reconcile_recovery_run(
    settings: Settings, recovery_block_id: uuid.UUID
) -> dict[str, Any] | None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.recovery_run_id,r.trial_id,r.status,t.state,
                      t.completed_at,t.failure_type
               FROM charm_control.experiment_v2_screening_recovery_runs r
               JOIN charm_control.trials t USING(trial_id)
               WHERE r.recovery_block_id=%s AND r.status='CREATED'
               ORDER BY r.sequence LIMIT 1""",
            (recovery_block_id,),
        )
        row = cur.fetchone()
        if row is None or row["completed_at"] is None:
            return dict(row) if row is not None else None
        passed = row["state"] == "COMPLETED"
        status = "COMPLETED" if passed else "FAILED"
        failure_details = (
            {} if passed else {"trial_state": row["state"], "failure_type": row["failure_type"]}
        )
        cur.execute(
            """UPDATE charm_control.experiment_v2_screening_recovery_runs
               SET status=%s,failure_details=%s,completed_at=clock_timestamp()
               WHERE recovery_run_id=%s AND status='CREATED'""",
            (status, Jsonb(failure_details), row["recovery_run_id"]),
        )
        conn.commit()
    return {**dict(row), "run_status": status}


def _fail_recovery_infrastructure(
    settings: Settings,
    campaign_id: uuid.UUID,
    recovery_block_id: uuid.UUID,
    reconciled: dict[str, Any],
    owner: str,
) -> ScreeningStep:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_screening_recovery_blocks
               SET status='FAILED',completed_at=clock_timestamp()
               WHERE recovery_block_id=%s AND status NOT IN ('PASSED','BLOCKED','FAILED')""",
            (recovery_block_id,),
        )
        conn.commit()
    block = _recovery_block(settings, campaign_id)
    if block["campaign_status"] in {"RUNNING", "PAUSED"}:
        control_campaign(
            settings,
            campaign_id,
            "stop",
            f"screening-recovery infrastructure trial {reconciled['trial_id']} failed",
            actor=owner,
        )
    return ScreeningStep(
        "infrastructure-failed",
        campaign_id,
        recovery_block_id,
        uuid.UUID(str(reconciled["recovery_run_id"])),
        uuid.UUID(str(reconciled["trial_id"])),
        {"failure_type": reconciled.get("failure_type")},
    )


def run_screening_recovery_next(
    settings: Settings,
    campaign_id: uuid.UUID,
    *,
    owner: str = "v2-screening-recovery",
    lease_seconds: int = 600,
) -> ScreeningStep:
    block = _recovery_block(settings, campaign_id)
    recovery_block_id = uuid.UUID(str(block["recovery_block_id"]))
    reconciled = _reconcile_recovery_run(settings, recovery_block_id)
    infrastructure_failures = {"DATASET_RESTORE_FAILED", "BASELINE_FINGERPRINT_FAILED"}
    if reconciled is not None and reconciled.get("run_status") == "FAILED":
        if reconciled.get("failure_type") in infrastructure_failures:
            return _fail_recovery_infrastructure(
                settings, campaign_id, recovery_block_id, reconciled, owner
            )
        return ScreeningStep(
            "candidate-failure-retained",
            campaign_id,
            recovery_block_id,
            uuid.UUID(str(reconciled["recovery_run_id"])),
            uuid.UUID(str(reconciled["trial_id"])),
            {"failure_type": reconciled.get("failure_type")},
        )
    if block["campaign_status"] != "RUNNING":
        raise ValueError(
            f"screening-recovery campaign must be RUNNING, not {block['campaign_status']}"
        )
    transitions = {
        "RECOVERY_PLANNED": "RECOVERY_RUNNING",
        "OAT_PLANNED": "OAT_RUNNING",
    }
    if block["status"] in transitions:
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_screening_recovery_blocks
                   SET status=%s WHERE recovery_block_id=%s AND status=%s""",
                (transitions[block["status"]], recovery_block_id, block["status"]),
            )
            conn.commit()
        block["status"] = transitions[block["status"]]
    if block["status"] not in {"RECOVERY_RUNNING", "OAT_RUNNING"}:
        raise ValueError(f"screening-recovery block cannot run from {block['status']}")
    if reconciled is not None and reconciled.get("completed_at") is None:
        result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
        finalized = _reconcile_recovery_run(settings, recovery_block_id)
        if finalized is not None and finalized.get("run_status") == "FAILED":
            if finalized.get("failure_type") in infrastructure_failures:
                return _fail_recovery_infrastructure(
                    settings, campaign_id, recovery_block_id, finalized, owner
                )
            return ScreeningStep(
                "candidate-failure-retained",
                campaign_id,
                recovery_block_id,
                uuid.UUID(str(finalized["recovery_run_id"])),
                uuid.UUID(str(finalized["trial_id"])),
                {"failure_type": finalized.get("failure_type")},
            )
        return ScreeningStep(
            "run-executed",
            campaign_id,
            recovery_block_id,
            uuid.UUID(str(reconciled["recovery_run_id"])),
            result.trial_id,
            {"trial_state": result.state},
        )
    phase = "INITIAL" if block["status"] == "RECOVERY_RUNNING" else "OAT"
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_v2_screening_recovery_runs
               WHERE recovery_block_id=%s AND phase=%s AND status='PLANNED'
               ORDER BY sequence LIMIT 1""",
            (recovery_block_id, phase),
        )
        planned_row = cur.fetchone()
    if planned_row is None:
        next_status = "INITIAL_COMPLETE" if phase == "INITIAL" else "OAT_COMPLETE"
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_screening_recovery_blocks
                   SET status=%s WHERE recovery_block_id=%s AND status=%s""",
                (next_status, recovery_block_id, block["status"]),
            )
            conn.commit()
        control_campaign(
            settings,
            campaign_id,
            "pause",
            f"screening-recovery {phase.lower()} observations complete; analysis required",
            actor=owner,
        )
        return ScreeningStep(
            "phase-complete",
            campaign_id,
            recovery_block_id,
            None,
            None,
            {"phase": phase},
        )
    planned = dict(planned_row)
    run_id = uuid.UUID(str(planned["recovery_run_id"]))
    evaluation_role = (
        "SCREENING_DEFAULT_CONTROL"
        if planned["evaluation_kind"] == "DEFAULT_CONTROL"
        else "SCREENING_CANDIDATE"
    )
    trial_id = create_v2_tuned_benchmark_trial(
        settings,
        campaign_id,
        uuid.UUID(str(block["preflight_id"])),
        {str(name): str(value) for name, value in planned["requested_configuration"].items()},
        int(planned["random_seed"]),
        f"v2-screening-recovery:{run_id}",
        evidence_role="CALIBRATION",
        evaluation_role=evaluation_role,
        warmup_seconds=FROZEN_WARMUP_SECONDS,
        duration_seconds=FROZEN_MEASUREMENT_SECONDS,
        concurrency=FROZEN_CONCURRENCY,
        client_threads=FROZEN_CLIENT_THREADS,
        restore_mechanism=FROZEN_RESTORE_MECHANISM,
        benchmark_profile=FROZEN_PROFILE_ID,
        runtime_samples_required=True,
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_screening_recovery_runs
               SET status='CREATED',trial_id=%s
               WHERE recovery_run_id=%s AND status='PLANNED'""",
            (trial_id, run_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("screening-recovery run lost its PLANNED state")
        conn.commit()
    result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
    finalized = _reconcile_recovery_run(settings, recovery_block_id)
    if finalized is not None and finalized.get("run_status") == "FAILED":
        if finalized.get("failure_type") in infrastructure_failures:
            return _fail_recovery_infrastructure(
                settings, campaign_id, recovery_block_id, finalized, owner
            )
        return ScreeningStep(
            "candidate-failure-retained",
            campaign_id,
            recovery_block_id,
            run_id,
            trial_id,
            {"failure_type": finalized.get("failure_type")},
        )
    return ScreeningStep(
        "run-executed",
        campaign_id,
        recovery_block_id,
        run_id,
        trial_id,
        {
            "trial_state": result.state,
            "phase": phase,
            "chronological_execution_index": planned["chronological_execution_index"],
        },
    )


def screening_recovery_history(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    block = _recovery_block(settings, campaign_id)
    recovery_block_id = uuid.UUID(str(block["recovery_block_id"]))
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.*,t.state,t.objective_values,t.constraint_values,t.workflow_result,
                      t.started_at AS trial_started_at,t.completed_at AS trial_completed_at,
                      cr.restore_id,cr.duration_seconds AS restore_seconds,
                      cr.exact_core_passed,cr.physical_statistics_passed
               FROM charm_control.experiment_v2_screening_recovery_runs r
               LEFT JOIN charm_control.trials t USING(trial_id)
               LEFT JOIN charm_control.experiment_candidate_dataset_restores cr
                 ON cr.restore_id=t.candidate_dataset_restore_id
               WHERE r.recovery_block_id=%s ORDER BY r.sequence""",
            (recovery_block_id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        row["run_id"] = row["recovery_run_id"]
        for key, value in tuple(row.items()):
            if isinstance(value, uuid.UUID):
                row[key] = str(value)
        if row.get("trial_started_at") is not None and row.get("trial_completed_at") is not None:
            row["total_seconds"] = (
                row["trial_completed_at"] - row["trial_started_at"]
            ).total_seconds()
    block_payload = {
        key: str(value) if isinstance(value, uuid.UUID) else value for key, value in block.items()
    }
    return {"block": block_payload, "runs": rows}


def _source_screening_rows(settings: Settings) -> list[dict[str, Any]]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.*,t.state,t.objective_values,t.constraint_values,t.workflow_result,
                      t.started_at AS trial_started_at,t.completed_at AS trial_completed_at,
                      cr.restore_id,cr.duration_seconds AS restore_seconds,
                      cr.exact_core_passed,cr.physical_statistics_passed
               FROM charm_control.experiment_v2_screening_runs r
               LEFT JOIN charm_control.trials t USING(trial_id)
               LEFT JOIN charm_control.experiment_candidate_dataset_restores cr
                 ON cr.restore_id=t.candidate_dataset_restore_id
               WHERE r.block_id=%s ORDER BY r.sequence""",
            (SOURCE_BLOCK_ID,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        for key, value in tuple(row.items()):
            if isinstance(value, uuid.UUID):
                row[key] = str(value)
        if row.get("trial_started_at") is not None and row.get("trial_completed_at") is not None:
            row["total_seconds"] = (
                row["trial_completed_at"] - row["trial_started_at"]
            ).total_seconds()
    return rows


def _combined_initial_points(
    settings: Settings, recovery_history: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_rows = _source_screening_rows(settings)
    source_initial = [row for row in source_rows if int(row["sequence"]) <= 33]
    source_tail = [row for row in source_rows if int(row["sequence"]) in (34, 35)]
    recovery_initial = [row for row in recovery_history["runs"] if row["phase"] == "INITIAL"]
    if (
        [int(row["sequence"]) for row in source_initial] != list(range(1, 34))
        or any(row["status"] != "COMPLETED" for row in source_initial)
        or [int(row["sequence"]) for row in recovery_initial] != [34, 35]
    ):
        raise ValueError("combined screening analysis lacks its frozen 1-35 chronology")
    combined_rows = [*source_initial, *recovery_initial]
    lineage = {
        "source_block_id": str(SOURCE_BLOCK_ID),
        "source_positions": [1, 33],
        "recovery_block_id": recovery_history["block"]["recovery_block_id"],
        "recovery_positions": [34, 35],
        "retained_source_tail": [
            {
                "run_id": str(row["run_id"]),
                "sequence": int(row["sequence"]),
                "status": row["status"],
                "trial_id": str(row["trial_id"]) if row.get("trial_id") else None,
            }
            for row in source_tail
        ],
        "chronology_gap_not_adjusted": True,
    }
    return _analysis_points(combined_rows), lineage


def analyze_screening_recovery(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    history = screening_recovery_history(settings, campaign_id)
    block = history["block"]
    if block["campaign_status"] != "PAUSED":
        raise ValueError("screening-recovery analysis requires a clean paused campaign")
    if block["status"] not in {"INITIAL_COMPLETE", "OAT_COMPLETE"}:
        raise ValueError("screening-recovery analysis requires a completed phase")
    manifest_sha256, _ = recovery_manifest_payload()
    if manifest_sha256 != block["manifest_sha256"]:
        raise ValueError("screening-recovery manifest differs from the frozen campaign")
    source_manifest_sha256, screening_payload = screening_manifest_payload()
    if source_manifest_sha256 != block["source_manifest_sha256"]:
        raise ValueError("source screening manifest differs from D030 lineage")
    initial_points, lineage = _combined_initial_points(settings, history)
    oat_points = _analysis_points([row for row in history["runs"] if row["phase"] == "OAT"])
    initial_analysis = (
        dict(block["initial_analysis"]) if block["status"] == "OAT_COMPLETE" else None
    )
    analysis = {
        "campaign_id": str(campaign_id),
        "recovery_block_id": str(block["recovery_block_id"]),
        "source_screening_block_id": str(SOURCE_BLOCK_ID),
        "restore_validation_block_id": str(block["restore_validation_block_id"]),
        "benchmark_profile_id": block["benchmark_profile_id"],
        "source_manifest_sha256": source_manifest_sha256,
        "recovery_manifest_sha256": manifest_sha256,
        "recovery_schedule_sha256": block["recovery_schedule_sha256"],
        "analysis_plan": block["analysis_plan"],
        "combined_lineage": lineage,
        **analyze_screening_observations(
            [*initial_points, *oat_points],
            screening_payload,
            initial_analysis=initial_analysis,
        ),
    }
    analysis["analysis_sha256"] = _canonical_sha256(analysis)
    outcome = str(analysis["outcome"])
    recovery_block_id = uuid.UUID(str(block["recovery_block_id"]))
    terminal_status: str | None = None
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        if block["status"] == "INITIAL_COMPLETE" and outcome == "OAT_REQUIRED":
            oat_rows = screening_oat_plan_rows(
                screening_payload,
                recovery_block_id,
                campaign_id,
                list(analysis["oat_parameters"]),
            )
            for row in oat_rows:
                cur.execute(
                    """INSERT INTO charm_control.experiment_v2_screening_recovery_runs
                       (recovery_run_id,recovery_block_id,campaign_id,sequence,
                        chronological_execution_index,phase,evaluation_kind,oat_parameter,
                        random_seed,requested_configuration,status)
                       VALUES (%s,%s,%s,%s,%s,'OAT',%s,%s,%s,%s,'PLANNED')""",
                    (
                        row["run_id"],
                        recovery_block_id,
                        campaign_id,
                        row["sequence"],
                        row["chronological_execution_index"],
                        row["evaluation_kind"],
                        row["oat_parameter"],
                        row["random_seed"],
                        Jsonb(row["requested_configuration"]),
                    ),
                )
            cur.execute(
                """UPDATE charm_control.experiment_v2_screening_recovery_blocks
                   SET status='OAT_PLANNED',initial_analysis=%s,
                       initial_analysis_sha256=%s WHERE recovery_block_id=%s""",
                (Jsonb(analysis), analysis["analysis_sha256"], recovery_block_id),
            )
        else:
            terminal_status = "PASSED" if outcome == "PASSED" else "BLOCKED"
            if block["status"] == "INITIAL_COMPLETE":
                cur.execute(
                    """UPDATE charm_control.experiment_v2_screening_recovery_blocks
                       SET status=%s,initial_analysis=%s,initial_analysis_sha256=%s,
                           final_analysis=%s,final_analysis_sha256=%s,
                           completed_at=clock_timestamp() WHERE recovery_block_id=%s""",
                    (
                        terminal_status,
                        Jsonb(analysis),
                        analysis["analysis_sha256"],
                        Jsonb(analysis),
                        analysis["analysis_sha256"],
                        recovery_block_id,
                    ),
                )
            else:
                cur.execute(
                    """UPDATE charm_control.experiment_v2_screening_recovery_blocks
                       SET status=%s,final_analysis=%s,final_analysis_sha256=%s,
                           completed_at=clock_timestamp() WHERE recovery_block_id=%s""",
                    (
                        terminal_status,
                        Jsonb(analysis),
                        analysis["analysis_sha256"],
                        recovery_block_id,
                    ),
                )
        conn.commit()
    if terminal_status is not None:
        control_campaign(
            settings,
            campaign_id,
            "stop",
            f"parameter screening recovery resolved as {terminal_status}",
            actor="v2-screening-recovery-analysis",
        )
    analysis["block_status"] = terminal_status or "OAT_PLANNED"
    analysis["next_action"] = (
        "explicitly resume the recovery campaign for the frozen OAT pairs"
        if terminal_status is None
        else (
            "screening passed; record final knob decisions before primary protocol freeze"
            if terminal_status == "PASSED"
            else "retain the blocked drift, validity, or selection outcome without more runs"
        )
    )
    return analysis


def export_screening_recovery_analysis(
    settings: Settings,
    campaign_id: uuid.UUID,
    output: Path | None = None,
) -> dict[str, str]:
    block = _recovery_block(settings, campaign_id)
    analysis = block.get("final_analysis")
    analysis_sha256 = block.get("final_analysis_sha256")
    if block["status"] not in {"PASSED", "BLOCKED"} or not isinstance(analysis, dict):
        raise ValueError("screening-recovery export requires a finalized analysis")
    if not isinstance(analysis_sha256, str) or len(analysis_sha256) != 64:
        raise ValueError("screening-recovery analysis has no valid SHA-256")
    hash_payload = dict(analysis)
    embedded_sha256 = hash_payload.pop("analysis_sha256", None)
    if embedded_sha256 != analysis_sha256 or _canonical_sha256(hash_payload) != analysis_sha256:
        raise ValueError("persisted screening-recovery analysis hash does not verify")
    _, payload = recovery_manifest_payload()
    artifact_root = Path(str(payload["artifact_root"])).resolve()
    destination = (
        output.resolve()
        if output is not None
        else (
            artifact_root / f"screening-recovery-analysis-{block['recovery_block_id']}.json"
        ).resolve()
    )
    try:
        destination.relative_to(artifact_root)
    except ValueError as error:
        raise ValueError("screening-recovery export must stay under its artifact root") from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(analysis, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return {
        "campaign_id": str(campaign_id),
        "recovery_block_id": str(block["recovery_block_id"]),
        "analysis_sha256": analysis_sha256,
        "output_path": str(destination),
    }
