from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

from charmdb.config import Settings
from charmdb.db import connect
from charmdb.protocol import PROTOCOL_ID, load_manifest
from charmdb.provenance import source_tree_sha256

EXPECTED_IMAGE = "postgres@sha256:5773fe724c49c42a7a9ca70202e11e1dff21fb7235b335a73f39297d200b73a2"
EXPECTED_EXTENSIONS = frozenset({"pg_stat_statements", "pg_buffercache", "pg_prewarm"})
EXPECTED_MIGRATIONS = tuple(f"{number:03d}" for number in range(1, 26))
CheckStatus = Literal["PASS", "FAIL", "WARN"]
BootstrapPhase = Literal["preinitialize", "postinitialize"]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class BootstrapCheck:
    name: str
    status: CheckStatus
    summary: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class BootstrapReport:
    protocol_id: str
    phase: BootstrapPhase
    captured_at: str
    repository_root: str
    passed: bool
    safe_to_initialize: bool
    machine: dict[str, Any]
    software: dict[str, Any]
    provenance: dict[str, Any]
    checks: tuple[BootstrapCheck, ...]


CommandRunner = Callable[[list[str]], CommandResult]
DatabaseProbe = Callable[[Settings], dict[str, Any]]


def _run(command: list[str]) -> CommandResult:
    try:
        completed = subprocess.run(
            command,
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return CommandResult(127, "", type(error).__name__)
    return CommandResult(completed.returncode, completed.stdout.strip(), completed.stderr.strip())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _migration_sha256(path: Path) -> str:
    """Match the normalized-text hashing used by ``db.apply_migrations``."""
    body = path.read_text(encoding="utf-8")
    return hashlib.sha256(body.encode()).hexdigest()


def _check(
    name: str,
    passed: bool,
    success: str,
    failure: str,
    evidence: dict[str, Any] | None = None,
) -> BootstrapCheck:
    return BootstrapCheck(
        name,
        "PASS" if passed else "FAIL",
        success if passed else failure,
        evidence or {},
    )


def _warning(name: str, summary: str, evidence: dict[str, Any] | None = None) -> BootstrapCheck:
    return BootstrapCheck(name, "WARN", summary, evidence or {})


def _physical_memory_bytes() -> int | None:
    if sys.platform == "win32":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.total_physical)
        except (AttributeError, OSError):
            return None
    if hasattr(os, "sysconf"):
        try:
            return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
        except (OSError, ValueError):
            return None
    return None


def _machine(root: Path) -> dict[str, Any]:
    disk = shutil.disk_usage(root)
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "architecture": platform.machine(),
        "processor": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "physical_memory_bytes": _physical_memory_bytes(),
        "workspace_disk": {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
        },
    }


def _software(runner: CommandRunner) -> dict[str, Any]:
    uv = runner(["uv", "--version"])
    docker = runner(["docker", "version", "--format", "{{json .}}"])
    compose = runner(["docker", "compose", "version", "--short"])
    docker_payload: dict[str, Any] = {}
    if docker.returncode == 0:
        try:
            raw = json.loads(docker.stdout)
            docker_payload = {
                "client_version": (raw.get("Client") or {}).get("Version"),
                "server_version": (raw.get("Server") or {}).get("Version"),
                "server_os": (raw.get("Server") or {}).get("Os"),
                "server_arch": (raw.get("Server") or {}).get("Arch"),
            }
        except json.JSONDecodeError:
            docker_payload = {"error": "invalid docker version JSON"}
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "uv": uv.stdout if uv.returncode == 0 else None,
        "docker": docker_payload,
        "docker_compose": compose.stdout if compose.returncode == 0 else None,
    }


def _migration_hashes(root: Path) -> dict[str, str]:
    return {
        path.stem: _migration_sha256(path)
        for path in sorted((root / "migrations").glob("*.sql"))
        if path.stem[:3] in EXPECTED_MIGRATIONS
    }


def _git_provenance(root: Path, runner: CommandRunner) -> tuple[dict[str, Any], BootstrapCheck]:
    commit = runner(["git", "rev-parse", "HEAD"])
    status = runner(["git", "status", "--porcelain", "--untracked-files=no"])
    available = commit.returncode == 0 and len(commit.stdout) == 40
    dirty = status.returncode == 0 and bool(status.stdout)
    evidence = {
        "commit": commit.stdout if available else None,
        "tracked_worktree_dirty": dirty if status.returncode == 0 else None,
    }
    if not available:
        return evidence, _check(
            "git-provenance",
            False,
            "Git provenance captured",
            "Git commit could not be resolved",
            evidence,
        )
    if dirty:
        return evidence, _warning(
            "git-provenance",
            "Git commit captured, but tracked files have uncommitted changes",
            evidence,
        )
    return evidence, _check(
        "git-provenance",
        True,
        "Git commit captured and tracked worktree is clean",
        "",
        evidence,
    )


def _compose_check(runner: CommandRunner) -> tuple[BootstrapCheck, dict[str, Any]]:
    result = runner(["docker", "compose", "config", "--format", "json"])
    if result.returncode != 0:
        return (
            _check(
                "compose-protocol",
                False,
                "Compose protocol matches v2",
                "Docker Compose configuration could not be rendered",
                {"error": result.stderr[:200]},
            ),
            {},
        )
    try:
        payload = json.loads(result.stdout)
        service = payload["services"]["target-postgres"]
        volume = payload["volumes"]["charm_target_data"]["name"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        return (
            _check(
                "compose-protocol",
                False,
                "Compose protocol matches v2",
                "Docker Compose configuration is malformed",
                {"error": type(error).__name__},
            ),
            {},
        )
    command = [str(item) for item in service.get("command", [])]
    evidence = {
        "image": service.get("image"),
        "cpus": service.get("cpus"),
        "memory_bytes": int(service.get("mem_limit") or 0),
        "shared_memory_bytes": int(service.get("shm_size") or 0),
        "pids_limit": int(service.get("pids_limit") or 0),
        "data_checksums": "--data-checksums"
        in str((service.get("environment") or {}).get("POSTGRES_INITDB_ARGS", "")),
        "shared_preload_libraries": "shared_preload_libraries=pg_stat_statements" in command,
        "volume_name": volume,
    }
    matches = evidence == {
        "image": EXPECTED_IMAGE,
        "cpus": 4,
        "memory_bytes": 4 * 1024**3,
        "shared_memory_bytes": 1024**3,
        "pids_limit": 512,
        "data_checksums": True,
        "shared_preload_libraries": True,
        "volume_name": "charm_target_data",
    }
    return (
        _check(
            "compose-protocol",
            matches,
            "Pinned image and target resource limits match protocol v2",
            "Pinned image or target resource limits differ from protocol v2",
            evidence,
        ),
        evidence,
    )


def _docker_state(
    runner: CommandRunner, phase: BootstrapPhase, volume_name: str
) -> tuple[list[BootstrapCheck], dict[str, Any]]:
    version = runner(["docker", "version", "--format", "{{json .}}"])
    daemon = _check(
        "docker-daemon",
        version.returncode == 0,
        "Docker daemon is reachable",
        "Docker daemon is not reachable",
    )
    if version.returncode != 0:
        return [daemon], {"volume_present": None, "container_running": None}
    volume = runner(["docker", "volume", "inspect", volume_name])
    container = runner(["docker", "compose", "ps", "-q", "target-postgres"])
    volume_present = volume.returncode == 0
    container_running = container.returncode == 0 and bool(container.stdout.strip())
    expect_present = phase == "postinitialize"
    state = {
        "volume_present": volume_present,
        "container_running": container_running,
        "volume_name": volume_name,
    }
    return [
        daemon,
        _check(
            "target-volume-state",
            volume_present == expect_present,
            (
                "Target volume exists after initialization"
                if expect_present
                else "Target volume is absent and safe to initialize"
            ),
            (
                "Target volume is missing after initialization"
                if expect_present
                else "Target volume already exists; freshness is not proven"
            ),
            state,
        ),
        _check(
            "target-container-state",
            container_running == expect_present,
            (
                "Target container is running after initialization"
                if expect_present
                else "Target container is absent before initialization"
            ),
            (
                "Target container is not running after initialization"
                if expect_present
                else "Target container is already running before initialization"
            ),
            state,
        ),
    ], state


def _dsn_identity(dsn: str) -> dict[str, Any]:
    parsed = urlsplit(dsn)
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port,
        "database": parsed.path.lstrip("/"),
        "user": parsed.username,
    }


def probe_control_database(settings: Settings) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_database() AS database, version() AS version")
        identity = dict(cur.fetchone() or {})
        cur.execute("SELECT to_regclass('charm_control.schema_migrations') AS relation")
        row = cur.fetchone()
        if row is None or row["relation"] is None:
            identity["migrations"] = {}
        else:
            cur.execute("SELECT version,sha256 FROM charm_control.schema_migrations")
            identity["migrations"] = {
                str(item["version"]): str(item["sha256"]) for item in cur.fetchall()
            }
    return identity


def probe_target_database(settings: Settings) -> dict[str, Any]:
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT current_database() AS database, version() AS version, "
            "current_setting('data_checksums') AS data_checksums"
        )
        identity = dict(cur.fetchone() or {})
        cur.execute("SELECT extname FROM pg_extension ORDER BY extname")
        identity["extensions"] = [str(item["extname"]) for item in cur.fetchall()]
    return identity


def _database_checks(
    settings: Settings,
    phase: BootstrapPhase,
    expected_hashes: dict[str, str],
    control_probe: DatabaseProbe,
    target_probe: DatabaseProbe,
) -> tuple[list[BootstrapCheck], dict[str, Any]]:
    control_identity = _dsn_identity(settings.control_dsn)
    target_identity = _dsn_identity(settings.target_dsn)
    target_fields = {
        "host": settings.target_host,
        "port": settings.target_port,
        "database": settings.target_db,
        "user": settings.target_user,
    }
    distinct = (
        control_identity.get("host"),
        control_identity.get("port"),
        control_identity.get("database"),
    ) != (
        target_identity.get("host"),
        target_identity.get("port"),
        target_identity.get("database"),
    )
    checks = [
        _check(
            "database-separation",
            distinct,
            "Control and target database endpoints are distinct",
            "Control and target database endpoints are identical",
            {"control": control_identity, "target": target_identity},
        ),
        _check(
            "target-dsn-consistency",
            target_identity
            == {
                "scheme": target_identity.get("scheme"),
                **target_fields,
            },
            "Target DSN identity matches decomposed target settings",
            "Target DSN identity conflicts with decomposed target settings",
            {"target_dsn": target_identity, "target_fields": target_fields},
        ),
    ]
    evidence: dict[str, Any] = {"control_dsn": control_identity, "target_dsn": target_identity}
    try:
        control = control_probe(settings)
        applied = dict(control.get("migrations") or {})
        migrations_match = all(applied.get(key) == value for key, value in expected_hashes.items())
        checks.append(
            _check(
                "control-database",
                migrations_match and len(expected_hashes) == len(EXPECTED_MIGRATIONS),
                "Control database is reachable with unchanged migrations 001-025",
                "Control database is unreachable, unmigrated, or has migration hash drift",
                {
                    "database": control.get("database"),
                    "applied_expected_migrations": sum(
                        applied.get(key) == value for key, value in expected_hashes.items()
                    ),
                },
            )
        )
        evidence["control"] = {
            "database": control.get("database"),
            "version": control.get("version"),
        }
    except Exception as error:
        checks.append(
            _check(
                "control-database",
                False,
                "Control database is ready",
                "Control database probe failed",
                {"error_type": type(error).__name__},
            )
        )
    if phase == "postinitialize":
        try:
            target = target_probe(settings)
            extensions = set(target.get("extensions") or [])
            target_valid = (
                target.get("data_checksums") == "on" and extensions >= EXPECTED_EXTENSIONS
            )
            checks.append(
                _check(
                    "target-database",
                    target_valid,
                    "Target database has checksums and required extensions",
                    "Target database is missing checksums or required extensions",
                    {
                        "database": target.get("database"),
                        "data_checksums": target.get("data_checksums"),
                        "required_extensions_present": sorted(EXPECTED_EXTENSIONS & extensions),
                    },
                )
            )
            evidence["target"] = {
                "database": target.get("database"),
                "version": target.get("version"),
            }
        except Exception as error:
            checks.append(
                _check(
                    "target-database",
                    False,
                    "Target database is ready",
                    "Target database probe failed",
                    {"error_type": type(error).__name__},
                )
            )
    return checks, evidence


def _freshness_check(report_path: Path | None) -> BootstrapCheck:
    if report_path is None:
        return _check(
            "freshness-evidence",
            False,
            "Pre-initialization freshness evidence is valid",
            "Post-initialization validation requires a pre-initialization report",
        )
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        valid = (
            payload.get("protocol_id") == PROTOCOL_ID
            and payload.get("phase") == "preinitialize"
            and payload.get("safe_to_initialize") is True
        )
    except (OSError, json.JSONDecodeError):
        valid = False
    return _check(
        "freshness-evidence",
        valid,
        "Pre-initialization report proves the target volume was absent",
        "Pre-initialization report does not prove target-volume freshness",
        {"path": str(report_path)},
    )


def run_bootstrap_preflight(
    settings: Settings,
    *,
    root: Path | None = None,
    manifest_path: Path | None = None,
    phase: BootstrapPhase = "preinitialize",
    freshness_report: Path | None = None,
    runner: CommandRunner = _run,
    control_probe: DatabaseProbe = probe_control_database,
    target_probe: DatabaseProbe = probe_target_database,
) -> BootstrapReport:
    repository_root = (root or Path.cwd()).resolve()
    manifest_file = manifest_path or (
        repository_root / "experiments/thesis/manifests/new-machine-bootstrap.json"
    )
    manifest = load_manifest(manifest_file)
    checks: list[BootstrapCheck] = []
    checks.append(
        _check(
            "bootstrap-manifest",
            manifest.stage == "new-machine-bootstrap"
            and manifest.evidence_role == "INFRASTRUCTURE",
            "Bootstrap manifest identifies protocol v2 infrastructure evidence",
            "Bootstrap manifest has the wrong stage or evidence role",
            {"path": str(manifest_file), "status": manifest.status},
        )
    )
    configured_artifacts = settings.artifact_dir.resolve()
    expected_artifacts = (repository_root / "artifacts-newpc/v2").resolve()
    isolated = (
        configured_artifacts == expected_artifacts
        or expected_artifacts in configured_artifacts.parents
    )
    checks.append(
        _check(
            "artifact-root-isolation",
            isolated,
            "Artifacts are isolated under artifacts-newpc/v2",
            "CHARMDB_ARTIFACT_DIR must be moved from legacy artifacts to artifacts-newpc/v2",
            {"configured": str(configured_artifacts), "required_root": str(expected_artifacts)},
        )
    )
    python_supported = (3, 12) <= sys.version_info[:2] < (3, 14)
    checks.append(
        _check(
            "python-version",
            python_supported,
            "Python version satisfies the locked project range",
            "Python version is outside the locked project range",
            {"version": platform.python_version()},
        )
    )
    lock_path = repository_root / "uv.lock"
    compose_path = repository_root / "docker-compose.yml"
    required_files = lock_path.is_file() and compose_path.is_file()
    checks.append(
        _check(
            "locked-inputs",
            required_files,
            "Locked dependencies and Compose definition are present",
            "uv.lock or docker-compose.yml is missing",
        )
    )
    migration_hashes = _migration_hashes(repository_root)
    checks.append(
        _check(
            "migration-baseline",
            tuple(sorted(name[:3] for name in migration_hashes)) == EXPECTED_MIGRATIONS,
            "Historical migration baseline 001-025 is complete",
            "Historical migration baseline 001-025 is incomplete",
            {"versions": sorted(migration_hashes)},
        )
    )
    git, git_check = _git_provenance(repository_root, runner)
    checks.append(git_check)
    compose_check, compose = _compose_check(runner)
    checks.append(compose_check)
    docker_checks, docker_state = _docker_state(
        runner, phase, str(compose.get("volume_name") or "charm_target_data")
    )
    checks.extend(docker_checks)
    database_checks, database = _database_checks(
        settings,
        phase,
        migration_hashes,
        control_probe,
        target_probe,
    )
    checks.extend(database_checks)
    if phase == "postinitialize":
        checks.append(_freshness_check(freshness_report))
    passed = all(item.status != "FAIL" for item in checks)
    safe_to_initialize = phase == "preinitialize" and all(
        item.status != "FAIL"
        for item in checks
        if item.name
        in {
            "bootstrap-manifest",
            "artifact-root-isolation",
            "python-version",
            "locked-inputs",
            "migration-baseline",
            "compose-protocol",
            "docker-daemon",
            "target-volume-state",
            "target-container-state",
            "database-separation",
            "target-dsn-consistency",
            "control-database",
        }
    )
    provenance = {
        "git": git,
        "source_tree_sha256": source_tree_sha256(repository_root),
        "uv_lock_sha256": _sha256(lock_path) if lock_path.is_file() else None,
        "docker_compose_sha256": _sha256(compose_path) if compose_path.is_file() else None,
        "bootstrap_manifest_sha256": _sha256(manifest_file),
        "migrations_001_025": migration_hashes,
        "compose": compose,
        "docker_state": docker_state,
        "database": database,
    }
    return BootstrapReport(
        protocol_id=PROTOCOL_ID,
        phase=phase,
        captured_at=datetime.now(UTC).isoformat(),
        repository_root=str(repository_root),
        passed=passed,
        safe_to_initialize=safe_to_initialize,
        machine=_machine(repository_root),
        software=_software(runner),
        provenance=provenance,
        checks=tuple(checks),
    )


def bootstrap_report_dict(report: BootstrapReport) -> dict[str, Any]:
    return asdict(report)


def write_bootstrap_report(report: BootstrapReport, output_path: Path) -> Path:
    resolved = output_path.resolve()
    root = Path(report.repository_root).resolve()
    allowed_root = (root / PurePosixPath("artifacts-newpc/v2")).resolve()
    if allowed_root != resolved.parent and allowed_root not in resolved.parents:
        raise ValueError("bootstrap reports must be written under artifacts-newpc/v2")
    if resolved.exists():
        raise FileExistsError(f"refusing to overwrite bootstrap evidence: {resolved}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_suffix(resolved.suffix + ".tmp")
    temporary.write_text(
        json.dumps(bootstrap_report_dict(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(resolved)
    return resolved
