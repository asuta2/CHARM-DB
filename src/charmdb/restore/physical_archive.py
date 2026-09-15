from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.db import connect
from charmdb.protocol import PROTOCOL_ID
from charmdb.restore.capability import (
    TARGET_DATA_DESTINATION,
    TARGET_SERVICE,
    run_docker,
)
from charmdb.restore.fingerprint import (
    capture_live_fingerprint,
    compare_fingerprints,
    standardize_logical_restore_state,
)
from charmdb.restore.models import CandidateBaseline

PHYSICAL_ARCHIVE_FILENAME_PREFIX = "physical-canonical"


@dataclass(frozen=True)
class PhysicalArchive:
    archive_id: uuid.UUID
    baseline_id: uuid.UUID
    assessment_id: uuid.UUID
    preflight_id: uuid.UUID
    source_validation_id: uuid.UUID
    volume_name: str
    image_digest: str
    archive_path: Path
    archive_sha256: str
    archive_byte_size: int
    duration_seconds: float
    exact_core: dict[str, Any]
    physical_statistics: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _baseline_and_assessment(
    settings: Settings,
    baseline_id: uuid.UUID,
    assessment_id: uuid.UUID,
) -> tuple[CandidateBaseline, dict[str, Any]]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT baseline_id,preflight_id,validation_id,restore_mechanism,approved,
                      exact_core,physical_statistics,physical_tolerances
               FROM charm_control.experiment_candidate_dataset_baselines
               WHERE baseline_id=%s""",
            (baseline_id,),
        )
        baseline_row = cur.fetchone()
        cur.execute(
            """SELECT assessment_id,image_digest,volume_name,volume_driver,
                      filesystem_type,mechanisms,evidence
               FROM charm_control.experiment_restore_capability_assessments
               WHERE assessment_id=%s""",
            (assessment_id,),
        )
        assessment_row = cur.fetchone()
    if baseline_row is None or not bool(baseline_row["approved"]):
        raise ValueError("physical archive creation requires an approved baseline")
    if assessment_row is None:
        raise ValueError("unknown restore capability assessment")
    mechanisms = dict(assessment_row["mechanisms"])
    physical = dict(mechanisms.get("physical-archive") or {})
    if not bool(physical.get("eligible")):
        raise ValueError("capability assessment did not approve physical archive")
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
    return baseline, dict(assessment_row)


def _target_volume_identity(*, allow_stopped: bool = False) -> tuple[str, str, str, bool]:
    container_id = run_docker(["compose", "ps", "-q", TARGET_SERVICE])
    if not container_id and allow_stopped:
        container_id = run_docker(["compose", "ps", "-a", "-q", TARGET_SERVICE])
    if not container_id:
        raise RuntimeError("target-postgres must be running before a physical volume operation")
    container = json.loads(run_docker(["inspect", container_id, "--format", "{{json .}}"]))
    mounts = [
        dict(item)
        for item in container.get("Mounts", [])
        if item.get("Destination") == TARGET_DATA_DESTINATION
    ]
    if len(mounts) != 1 or mounts[0].get("Type") != "volume":
        raise RuntimeError("target PostgreSQL data is not an unambiguous named volume")
    running = bool(dict(container.get("State") or {}).get("Running"))
    return container_id, str(container.get("Image")), str(mounts[0].get("Name")), running


def _control_plane_restore_safety(
    settings: Settings,
    permitted_campaign_id: uuid.UUID | None,
) -> None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) AS count FROM charm_control.campaigns
               WHERE status IN ('CREATED','RUNNING','PAUSED','STOPPING')
                 AND (%s::uuid IS NULL OR campaign_id<>%s)""",
            (permitted_campaign_id, permitted_campaign_id),
        )
        row = cur.fetchone()
    if row is None or int(row["count"]) != 0:
        raise RuntimeError("physical restore found another active campaign")


def stopped_target_fingerprint() -> dict[str, Any]:
    container_id, image_digest, volume_name, running = _target_volume_identity(allow_stopped=True)
    return {
        "target_available": running,
        "container_id": container_id,
        "image_digest": image_digest,
        "volume_name": volume_name,
        "state": "running" if running else "stopped",
    }


def _stop_target() -> None:
    run_docker(["compose", "stop", "--timeout", "60", TARGET_SERVICE], timeout=90)
    container_id = run_docker(["compose", "ps", "-a", "-q", TARGET_SERVICE])
    running = run_docker(["inspect", container_id, "--format", "{{.State.Running}}"])
    if running.lower() != "false":
        raise RuntimeError("target-postgres did not stop cleanly")


def _start_target() -> None:
    run_docker(["compose", "up", "-d", "--wait", TARGET_SERVICE], timeout=180)


def _archive_mount_argument(path: Path, target: str, *, readonly: bool = False) -> str:
    suffix = ",readonly" if readonly else ""
    return f"type=bind,source={path.resolve()},target={target}{suffix}"


def _archive_volume(
    volume_name: str,
    image_digest: str,
    output_dir: Path,
    filename: str,
) -> None:
    run_docker(
        [
            "run",
            "--rm",
            "--user",
            "0:0",
            "--mount",
            f"type=volume,source={volume_name},target=/source,readonly",
            "--mount",
            _archive_mount_argument(output_dir, "/backup"),
            "--entrypoint",
            "tar",
            image_digest,
            "-C",
            "/source",
            "-cf",
            f"/backup/{filename}",
            ".",
        ],
        timeout=1800,
    )


def _restore_volume(
    volume_name: str,
    image_digest: str,
    archive_dir: Path,
    filename: str,
) -> None:
    run_docker(
        [
            "run",
            "--rm",
            "--user",
            "0:0",
            "--mount",
            f"type=volume,source={volume_name},target=/target",
            "--mount",
            _archive_mount_argument(archive_dir, "/backup", readonly=True),
            "--entrypoint",
            "sh",
            image_digest,
            "-ceu",
            (
                "find /target -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; "
                f"tar -xf /backup/{filename} -C /target"
            ),
        ],
        timeout=1800,
    )


def _record_current_dataset_validation(
    settings: Settings,
    preflight_id: uuid.UUID,
    mechanism: str,
) -> Any:
    from charmdb.restore.preflight import (
        DatasetRestoreResult,
        _content_identity,
        _run_schema_dump,
    )

    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT dataset_snapshot_sha256,schema_sha256,dataset_content_sha256,
                      evidence,unresolved
               FROM charm_control.experiment_manifest_preflights WHERE preflight_id=%s""",
            (preflight_id,),
        )
        preflight = cur.fetchone()
    if preflight is None:
        raise ValueError(f"unknown manifest preflight {preflight_id}")
    _schema_text, observed_schema = _run_schema_dump(settings)
    with connect(settings.target_dsn) as conn:
        observed_content, observed_rows = _content_identity(conn)
        conn.commit()
    expected_rows = dict(dict(preflight["evidence"])["dataset"])["relation_rows"]
    passed = (
        observed_schema == str(preflight["schema_sha256"])
        and observed_content == str(preflight["dataset_content_sha256"])
        and observed_rows == expected_rows
    )
    validation_id = uuid.uuid4()
    details = {
        "restore_mechanism": mechanism,
        "observed_rows": observed_rows,
        "expected_rows": expected_rows,
    }
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_dataset_restore_validations
               (validation_id,preflight_id,expected_snapshot_sha256,
                expected_schema_sha256,expected_content_sha256,observed_schema_sha256,
                observed_content_sha256,passed,details)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                validation_id,
                preflight_id,
                preflight["dataset_snapshot_sha256"],
                preflight["schema_sha256"],
                preflight["dataset_content_sha256"],
                observed_schema,
                observed_content,
                passed,
                Jsonb(details),
            ),
        )
        conn.commit()
    if not passed:
        raise RuntimeError("physical archive restore failed schema/content validation")
    unresolved = tuple(str(item) for item in preflight["unresolved"])
    return DatasetRestoreResult(
        validation_id,
        preflight_id,
        True,
        observed_schema,
        observed_content,
        not unresolved,
        unresolved,
    )


def create_physical_archive(
    settings: Settings,
    baseline_id: uuid.UUID,
    assessment_id: uuid.UUID,
) -> PhysicalArchive:
    from charmdb.restore.preflight import validate_dataset_restore

    baseline, assessment = _baseline_and_assessment(settings, baseline_id, assessment_id)
    started = time.monotonic()
    validation = validate_dataset_restore(settings, baseline.preflight_id)
    standardize_logical_restore_state(settings)
    exact_core, physical_statistics = capture_live_fingerprint(
        settings, baseline.preflight_id, validation.validation_id
    )
    comparison = compare_fingerprints(
        baseline.exact_core,
        baseline.physical_statistics,
        exact_core,
        physical_statistics,
        baseline.physical_tolerances,
    )
    if not comparison.passed:
        raise RuntimeError("physical archive source does not match the approved baseline")
    _container_id, image_digest, volume_name, running = _target_volume_identity()
    if not running:
        raise RuntimeError("target stopped before physical archive creation")
    if image_digest != str(assessment["image_digest"]):
        raise RuntimeError("target image differs from the capability assessment")
    if volume_name != str(assessment["volume_name"]):
        raise RuntimeError("target volume differs from the capability assessment")
    archive_id = uuid.uuid4()
    output_dir = settings.artifact_dir / "candidate-restore-pilot" / "physical-archives"
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{PHYSICAL_ARCHIVE_FILENAME_PREFIX}-{archive_id}.tar"
    archive_path = output_dir / filename
    stopped = False
    try:
        _stop_target()
        stopped = True
        _archive_volume(volume_name, image_digest, output_dir, filename)
    finally:
        if stopped:
            _start_target()
    if not archive_path.is_file() or archive_path.stat().st_size <= 0:
        raise RuntimeError("physical archive was not created")
    post_exact, post_physical = capture_live_fingerprint(
        settings, baseline.preflight_id, validation.validation_id
    )
    post_comparison = compare_fingerprints(
        exact_core,
        physical_statistics,
        post_exact,
        post_physical,
        baseline.physical_tolerances,
    )
    if not post_comparison.passed:
        raise RuntimeError("source fingerprint changed across physical archive creation")
    archive_sha256 = _sha256(archive_path)
    archive_size = archive_path.stat().st_size
    duration = time.monotonic() - started
    relative_path = archive_path.resolve().relative_to(settings.artifact_dir.resolve())
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_physical_dataset_archives
               (archive_id,protocol_id,baseline_id,assessment_id,preflight_id,
                source_validation_id,volume_name,image_digest,archive_relative_path,
                archive_sha256,archive_byte_size,exact_core,physical_statistics,status,
                started_at,completed_at,duration_seconds,details)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'VALIDATED',
                       clock_timestamp()-(%s*interval '1 second'),clock_timestamp(),%s,%s)""",
            (
                archive_id,
                PROTOCOL_ID,
                baseline.baseline_id,
                assessment_id,
                baseline.preflight_id,
                validation.validation_id,
                volume_name,
                image_digest,
                str(relative_path),
                archive_sha256,
                archive_size,
                Jsonb(exact_core),
                Jsonb(physical_statistics),
                duration,
                duration,
                Jsonb(
                    {
                        "source_comparison": asdict(comparison),
                        "post_start_comparison": asdict(post_comparison),
                    }
                ),
            ),
        )
        conn.commit()
    return PhysicalArchive(
        archive_id,
        baseline.baseline_id,
        assessment_id,
        baseline.preflight_id,
        validation.validation_id,
        volume_name,
        image_digest,
        archive_path,
        archive_sha256,
        archive_size,
        duration,
        exact_core,
        physical_statistics,
    )


def load_physical_archive(
    settings: Settings,
    archive_id: uuid.UUID,
    *,
    verify_artifact: bool = True,
) -> PhysicalArchive:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_physical_dataset_archives
               WHERE archive_id=%s AND status='VALIDATED'""",
            (archive_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError("unknown or unvalidated physical archive")
    path = (settings.artifact_dir / str(row["archive_relative_path"])).resolve()
    try:
        path.relative_to(settings.artifact_dir.resolve())
    except ValueError as error:
        raise ValueError("physical archive path escapes the artifact root") from error
    if verify_artifact and (
        not path.is_file()
        or path.stat().st_size != int(row["archive_byte_size"])
        or _sha256(path) != str(row["archive_sha256"])
    ):
        raise RuntimeError("physical archive artifact failed size/SHA-256 validation")
    return PhysicalArchive(
        uuid.UUID(str(row["archive_id"])),
        uuid.UUID(str(row["baseline_id"])),
        uuid.UUID(str(row["assessment_id"])),
        uuid.UUID(str(row["preflight_id"])),
        uuid.UUID(str(row["source_validation_id"])),
        str(row["volume_name"]),
        str(row["image_digest"]),
        path,
        str(row["archive_sha256"]),
        int(row["archive_byte_size"]),
        float(row["duration_seconds"]),
        dict(row["exact_core"]),
        dict(row["physical_statistics"]),
    )


def restore_physical_archive(
    settings: Settings,
    archive_id: uuid.UUID,
    *,
    permitted_campaign_id: uuid.UUID | None = None,
) -> Any:
    from charmdb.restore.preflight import _target_safety

    archive = load_physical_archive(settings, archive_id)
    _container_id, image_digest, volume_name, running = _target_volume_identity(allow_stopped=True)
    if image_digest != archive.image_digest or volume_name != archive.volume_name:
        raise RuntimeError("current target image/volume differs from the physical archive")
    if running:
        _target_safety(settings, permitted_campaign_id=permitted_campaign_id)
        _stop_target()
    else:
        _control_plane_restore_safety(settings, permitted_campaign_id)
    restored = False
    try:
        _restore_volume(
            archive.volume_name,
            archive.image_digest,
            archive.archive_path.parent,
            archive.archive_path.name,
        )
        restored = True
    finally:
        if restored:
            _start_target()
    return _record_current_dataset_validation(settings, archive.preflight_id, "physical-archive")


def physical_archive_dict(result: PhysicalArchive) -> dict[str, Any]:
    payload = asdict(result)
    for key in (
        "archive_id",
        "baseline_id",
        "assessment_id",
        "preflight_id",
        "source_validation_id",
    ):
        payload[key] = str(payload[key])
    payload["archive_path"] = str(result.archive_path)
    return payload


def physical_archive_history(
    settings: Settings,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 1000:
        raise ValueError("physical archive history limit must be between 1 and 1000")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT archive_id,baseline_id,assessment_id,preflight_id,
                      source_validation_id,volume_name,image_digest,archive_relative_path,
                      archive_sha256,archive_byte_size,status,duration_seconds,details,created_at
               FROM charm_control.experiment_physical_dataset_archives
               ORDER BY created_at DESC LIMIT %s""",
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]
