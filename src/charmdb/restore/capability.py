from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.db import connect
from charmdb.protocol import PROTOCOL_ID

COMPOSE_PROJECT = "charm-db"
TARGET_SERVICE = "target-postgres"
TARGET_DATA_DESTINATION = "/var/lib/postgresql"


@dataclass(frozen=True)
class MechanismCapability:
    eligible: bool
    reason: str
    safety_requirements: tuple[str, ...]


@dataclass(frozen=True)
class RestoreCapabilityAssessment:
    assessment_id: uuid.UUID
    captured_at: datetime
    container_id: str
    image_digest: str
    volume_name: str
    volume_driver: str
    volume_mountpoint: str
    filesystem_type: str
    mechanisms: dict[str, MechanismCapability]
    artifact_path: Path
    artifact_sha256: str
    artifact_byte_size: int


def run_docker(arguments: list[str], *, timeout: int = 30) -> str:
    completed = subprocess.run(
        ["docker", *arguments],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"Docker capability inspection failed for {' '.join(arguments)}: "
            f"{completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def evaluate_restore_capabilities(evidence: dict[str, Any]) -> dict[str, MechanismCapability]:
    mount = dict(evidence["target_mount"])
    engine = dict(evidence["docker_engine"])
    tools = dict(evidence["container_tools"])
    dedicated_volume = (
        mount.get("Type") == "volume"
        and mount.get("Destination") == TARGET_DATA_DESTINATION
        and bool(mount.get("Name"))
        and bool(mount.get("RW"))
    )
    local_driver = mount.get("Driver") == "local"
    archive_tools = bool(tools.get("tar")) and bool(tools.get("sha256sum"))
    logical_tools = bool(evidence.get("pg_dump")) and bool(evidence.get("pg_restore"))

    copy_on_write = MechanismCapability(
        False,
        (
            "Docker named volumes expose no verified atomic clone/snapshot primitive; "
            f"engine storage driver {engine.get('storage_driver')!r} does not clone the "
            "dedicated local volume"
        ),
        (
            "verified atomic stopped-volume snapshot API",
            "independent writable clone",
            "source immutability proof",
        ),
    )
    physical_archive = MechanismCapability(
        dedicated_volume and local_driver and archive_tools,
        (
            "dedicated local named volume and in-image archive tools are available"
            if dedicated_volume and local_driver and archive_tools
            else "dedicated writable local volume or required archive tools are unavailable"
        ),
        (
            "clean PostgreSQL stop",
            "read-only source mount during archive creation",
            "SHA-256 validation before destructive restore",
            "exact named-volume identity validation",
            "health and two-tier fingerprint verification after start",
        ),
    )
    logical_restore = MechanismCapability(
        logical_tools,
        "pg_dump and pg_restore are available" if logical_tools else "PostgreSQL tools missing",
        (
            "canonical base settings",
            "fixed restore options",
            "deterministic VACUUM/ANALYZE and CHECKPOINT",
            "two-tier fingerprint verification",
        ),
    )
    return {
        "copy-on-write": copy_on_write,
        "physical-archive": physical_archive,
        "logical-restore": logical_restore,
    }


def assess_restore_capabilities(
    settings: Settings,
    output_dir: Path | None = None,
) -> RestoreCapabilityAssessment:
    container_id = run_docker(["compose", "ps", "-q", TARGET_SERVICE])
    if not container_id:
        raise RuntimeError("target-postgres must be running for capability assessment")
    container = json.loads(run_docker(["inspect", container_id, "--format", "{{json .}}"]))
    mounts = [
        dict(item)
        for item in container.get("Mounts", [])
        if item.get("Destination") == TARGET_DATA_DESTINATION
    ]
    if len(mounts) != 1:
        raise RuntimeError("target must expose exactly one PostgreSQL data mount")
    mount = mounts[0]
    volume_name = str(mount.get("Name") or "")
    volume_rows = json.loads(run_docker(["volume", "inspect", volume_name]))
    if len(volume_rows) != 1:
        raise RuntimeError("target volume inspection returned an unexpected result")
    volume = dict(volume_rows[0])
    engine_raw = json.loads(run_docker(["info", "--format", "{{json .}}"]))
    filesystem = run_docker(
        ["exec", container_id, "sh", "-c", f"df -PT {TARGET_DATA_DESTINATION} | tail -1"]
    ).split()
    if len(filesystem) < 2:
        raise RuntimeError("target volume filesystem type could not be determined")
    tools = {
        name: bool(run_docker(["exec", container_id, "sh", "-c", f"command -v {name} || true"]))
        for name in ("tar", "sha256sum")
    }
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "protocol_id": PROTOCOL_ID,
        "captured_at": datetime.now(UTC).isoformat(),
        "host": {"platform": platform.platform(), "python": platform.python_version()},
        "compose": {"project": COMPOSE_PROJECT, "service": TARGET_SERVICE},
        "container": {
            "id": container_id,
            "image_digest": str(container.get("Image")),
            "state": dict(container.get("State") or {}),
        },
        "target_mount": mount,
        "volume": volume,
        "filesystem": {"type": filesystem[1], "df_fields": filesystem},
        "docker_engine": {
            "server_version": engine_raw.get("ServerVersion"),
            "operating_system": engine_raw.get("OperatingSystem"),
            "kernel_version": engine_raw.get("KernelVersion"),
            "architecture": engine_raw.get("Architecture"),
            "storage_driver": engine_raw.get("Driver"),
            "driver_status": engine_raw.get("DriverStatus"),
            "volume_plugins": dict(engine_raw.get("Plugins") or {}).get("Volume"),
            "live_restore_enabled": engine_raw.get("LiveRestoreEnabled"),
        },
        "container_tools": tools,
        "pg_dump": shutil.which("pg_dump"),
        "pg_restore": shutil.which("pg_restore"),
    }
    mechanisms = evaluate_restore_capabilities(evidence)
    evidence["mechanisms"] = {name: asdict(capability) for name, capability in mechanisms.items()}
    assessment_id = uuid.uuid4()
    evidence["assessment_id"] = str(assessment_id)
    destination = output_dir or (settings.artifact_dir / "candidate-restore-pilot" / "capabilities")
    destination.mkdir(parents=True, exist_ok=True)
    artifact_path = destination / f"restore-capabilities-{assessment_id}.json"
    artifact_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    artifact_bytes = artifact_path.read_bytes()
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    artifact_root = settings.artifact_dir.resolve()
    resolved_artifact = artifact_path.resolve()
    try:
        relative_path = resolved_artifact.relative_to(artifact_root)
    except ValueError as error:
        raise ValueError(
            "capability artifact must stay under the configured artifact root"
        ) from error
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_restore_capability_assessments
               (assessment_id,protocol_id,compose_project,compose_service,container_id,
                image_digest,volume_name,volume_driver,volume_mountpoint,filesystem_type,
                docker_engine,mechanisms,evidence,artifact_relative_path,artifact_sha256,
                artifact_byte_size)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                assessment_id,
                PROTOCOL_ID,
                COMPOSE_PROJECT,
                TARGET_SERVICE,
                container_id,
                str(container.get("Image")),
                volume_name,
                str(volume.get("Driver")),
                str(volume.get("Mountpoint")),
                filesystem[1],
                Jsonb(evidence["docker_engine"]),
                Jsonb(evidence["mechanisms"]),
                Jsonb(evidence),
                str(relative_path),
                artifact_sha256,
                len(artifact_bytes),
            ),
        )
        conn.commit()
    return RestoreCapabilityAssessment(
        assessment_id,
        datetime.fromisoformat(str(evidence["captured_at"])),
        container_id,
        str(container.get("Image")),
        volume_name,
        str(volume.get("Driver")),
        str(volume.get("Mountpoint")),
        filesystem[1],
        mechanisms,
        artifact_path,
        artifact_sha256,
        len(artifact_bytes),
    )


def capability_assessment_dict(result: RestoreCapabilityAssessment) -> dict[str, Any]:
    payload = asdict(result)
    payload["assessment_id"] = str(result.assessment_id)
    payload["captured_at"] = result.captured_at.isoformat()
    payload["artifact_path"] = str(result.artifact_path)
    return payload


def restore_capability_history(
    settings: Settings,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 1000:
        raise ValueError("restore capability history limit must be between 1 and 1000")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT assessment_id,protocol_id,compose_project,compose_service,
                      container_id,image_digest,volume_name,volume_driver,
                      filesystem_type,mechanisms,artifact_relative_path,artifact_sha256,
                      artifact_byte_size,created_at
               FROM charm_control.experiment_restore_capability_assessments
               ORDER BY created_at DESC LIMIT %s""",
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]
