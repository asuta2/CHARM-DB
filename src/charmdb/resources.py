from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from charmdb.config import Settings
from charmdb.db import connect


class ResourceLimitError(RuntimeError):
    pass


def _run_docker(command: list[str], timeout: int = 20) -> str:
    completed = subprocess.run(
        command,
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        raise ResourceLimitError(
            f"Docker resource inspection failed for {' '.join(command[:3])}: "
            f"{completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _target_container_id() -> str:
    container_id = _run_docker(["docker", "compose", "ps", "-q", "target-postgres"])
    if not container_id:
        raise ResourceLimitError("target-postgres container is not running")
    return container_id.splitlines()[0].strip()


def _target_disk_available(container_id: str) -> int:
    output = _run_docker(["docker", "exec", container_id, "df", "-Pk", "/var/lib/postgresql"])
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) < 2:
        raise ResourceLimitError(f"unexpected target df output: {output}")
    fields = lines[-1].split()
    if len(fields) < 6:
        raise ResourceLimitError(f"unexpected target df row: {lines[-1]}")
    return int(fields[3]) * 1024


def capture_resource_snapshot(settings: Settings) -> dict[str, Any]:
    artifact_root = settings.artifact_dir.resolve()
    artifact_disk = shutil.disk_usage(artifact_root)
    container_id = _target_container_id()
    state = json.loads(
        _run_docker(["docker", "inspect", container_id, "--format", "{{json .State}}"])
    )
    host_config = json.loads(
        _run_docker(["docker", "inspect", container_id, "--format", "{{json .HostConfig}}"])
    )
    image_id = _run_docker(["docker", "inspect", container_id, "--format", "{{.Image}}"])
    engine = json.loads(
        _run_docker(
            [
                "docker",
                "info",
                "--format",
                "{{json .}}",
            ]
        )
    )
    stats = json.loads(
        _run_docker(["docker", "stats", container_id, "--no-stream", "--format", "{{json .}}"])
    )
    memory_usage_text = str(stats["MemUsage"]).split("/")[0].strip()
    memory_units = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "TiB": 1024**4,
    }
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGT]?i?B)", memory_usage_text)
    if match is None:
        raise ResourceLimitError(f"unexpected Docker memory value: {memory_usage_text}")
    number, unit = match.groups()
    memory_usage_bytes = int(float(number) * memory_units[unit])
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())::bigint AS bytes")
        row = cur.fetchone()
        if row is None:
            raise ResourceLimitError("target database size query returned no row")
        database_size = int(row["bytes"])
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "artifact_disk": {
            "path": str(artifact_root),
            "total_bytes": artifact_disk.total,
            "used_bytes": artifact_disk.used,
            "free_bytes": artifact_disk.free,
        },
        "target_disk": {
            "path": "/var/lib/postgresql",
            "free_bytes": _target_disk_available(container_id),
            "database_size_bytes": database_size,
        },
        "container": {
            "id": container_id,
            "image_id": image_id,
            "status": state.get("Status"),
            "health": (state.get("Health") or {}).get("Status"),
            "oom_killed": bool(state.get("OOMKilled")),
            "memory_usage_bytes": memory_usage_bytes,
            "memory_limit_bytes": int(host_config.get("Memory") or 0),
            "cpu_limit_nanos": int(host_config.get("NanoCpus") or 0),
            "pids_limit": int(host_config.get("PidsLimit") or 0),
            "pids": int(stats.get("PIDs") or 0),
            "cpu_percent": str(stats.get("CPUPerc") or ""),
            "block_io": str(stats.get("BlockIO") or ""),
            "network_io": str(stats.get("NetIO") or ""),
            "docker_host_memory_bytes": int(
                _run_docker(["docker", "info", "--format", "{{json .MemTotal}}"])
            ),
        },
        "docker_engine": {
            "memory_total_bytes": int(engine.get("MemTotal") or 0),
            "cpus": int(engine.get("NCPU") or 0),
            "driver": str(engine.get("Driver") or ""),
            "server_version": str(engine.get("ServerVersion") or ""),
        },
    }


def validate_resource_snapshot(settings: Settings, snapshot: dict[str, Any]) -> None:
    artifact_free = int(snapshot["artifact_disk"]["free_bytes"])
    target_free = int(snapshot["target_disk"]["free_bytes"])
    container = snapshot["container"]
    if artifact_free < settings.min_host_free_bytes:
        raise ResourceLimitError(
            f"artifact disk free bytes {artifact_free} below {settings.min_host_free_bytes}"
        )
    if target_free < settings.min_target_free_bytes:
        raise ResourceLimitError(
            f"target disk free bytes {target_free} below {settings.min_target_free_bytes}"
        )
    if container["oom_killed"]:
        raise ResourceLimitError("target container reports OOMKilled=true")
    if container["status"] != "running" or container["health"] != "healthy":
        raise ResourceLimitError(
            f"target container is not healthy: status={container['status']} "
            f"health={container['health']}"
        )
    memory_limit = int(container["memory_limit_bytes"])
    memory_usage = int(container["memory_usage_bytes"])
    if memory_limit <= 0:
        raise ResourceLimitError("target container memory limit is absent")
    if int(snapshot["docker_engine"]["memory_total_bytes"]) <= 0:
        raise ResourceLimitError("Docker engine memory limit is absent")
    fraction = memory_usage / memory_limit
    if fraction >= settings.max_container_memory_fraction:
        raise ResourceLimitError(
            f"target memory fraction {fraction:.4f} reached safety limit "
            f"{settings.max_container_memory_fraction:.4f}"
        )


def capture_and_validate_resources(
    settings: Settings,
    health_settle_seconds: float = 15.0,
    health_poll_seconds: float = 0.5,
) -> dict[str, Any]:
    if health_settle_seconds < 0 or health_poll_seconds < 0:
        raise ValueError("resource health wait values cannot be negative")
    deadline = time.monotonic() + health_settle_seconds
    while True:
        snapshot = capture_resource_snapshot(settings)
        try:
            validate_resource_snapshot(settings, snapshot)
            return snapshot
        except ResourceLimitError:
            container = snapshot["container"]
            transient_startup = (
                container["status"] == "running" and container["health"] == "starting"
            )
            if not transient_startup or time.monotonic() >= deadline:
                raise
            time.sleep(health_poll_seconds)
