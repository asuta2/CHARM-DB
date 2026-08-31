from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from statistics import median
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.controller import discover_knobs
from charmdb.db import connect
from charmdb.experiment_execution import evaluate_prerequisite_gate
from charmdb.provenance import source_tree_sha256
from charmdb.resources import capture_and_validate_resources

PGBENCH_RELATIONS = (
    "public.pgbench_accounts",
    "public.pgbench_branches",
    "public.pgbench_history",
    "public.pgbench_tellers",
)
CANONICAL_SNAPSHOT_FILENAME = "pgbench-canonical.dump"
CONTENT_QUERIES = (
    ("public.pgbench_accounts", "SELECT * FROM public.pgbench_accounts ORDER BY aid"),
    ("public.pgbench_branches", "SELECT * FROM public.pgbench_branches ORDER BY bid"),
    (
        "public.pgbench_history",
        "SELECT * FROM public.pgbench_history ORDER BY tid,bid,aid,delta,mtime,filler",
    ),
    ("public.pgbench_tellers", "SELECT * FROM public.pgbench_tellers ORDER BY tid"),
)
FROZEN_CACHE_POLICY = "restored-arm-block-120s-warmup-no-manual-cache-drop"
FROZEN_PRACTICAL_MARGINS = {
    "throughput_relative": 0.05,
    "p99_ms_absolute": 1.0,
    "hypervolume_relative": 0.05,
}


@dataclass(frozen=True)
class ManifestPreflightResult:
    preflight_id: uuid.UUID
    dataset_snapshot_sha256: str
    schema_sha256: str
    snapshot_path: Path
    evidence_path: Path
    evidence_sha256: str
    eligible: bool
    unresolved: tuple[str, ...]


@dataclass(frozen=True)
class DatasetRestoreResult:
    validation_id: uuid.UUID
    preflight_id: uuid.UUID
    passed: bool
    observed_schema_sha256: str
    observed_content_sha256: str
    eligible: bool
    unresolved: tuple[str, ...]


def normalize_schema_dump(payload: str) -> str:
    lines = []
    for line in payload.replace("\r\n", "\n").splitlines():
        stripped = line.strip()
        if stripped.startswith(("\\restrict ", "\\unrestrict ")):
            continue
        if stripped.startswith(("-- Dumped from database version", "-- Dumped by pg_dump version")):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip() + "\n"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pg_dump_environment(settings: Settings) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PGPASSWORD"] = settings.target_password
    return environment


def _pg_dump_base(settings: Settings) -> list[str]:
    executable = shutil.which("pg_dump")
    if executable is None:
        raise RuntimeError("pg_dump is required for an immutable dataset snapshot")
    command = [
        executable,
        "--host",
        settings.target_host,
        "--port",
        str(settings.target_port),
        "--username",
        settings.target_user,
        "--dbname",
        settings.target_db,
        "--no-owner",
        "--no-privileges",
    ]
    for relation in PGBENCH_RELATIONS:
        command.extend(("--table", relation))
    return command


def _run_schema_dump(settings: Settings, snapshot_id: str | None = None) -> tuple[str, str]:
    snapshot_args = ["--snapshot", snapshot_id] if snapshot_id else []
    completed = subprocess.run(
        [*_pg_dump_base(settings), *snapshot_args, "--schema-only"],
        text=True,
        capture_output=True,
        env=_pg_dump_environment(settings),
        timeout=120,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"pg_dump schema capture failed: {completed.stderr.strip()}")
    normalized = normalize_schema_dump(completed.stdout)
    return normalized, hashlib.sha256(normalized.encode()).hexdigest()


def _create_snapshot(settings: Settings, destination: Path, snapshot_id: str) -> None:
    temporary = destination.with_suffix(".dump.tmp")
    completed = subprocess.run(
        [
            *_pg_dump_base(settings),
            "--snapshot",
            snapshot_id,
            "--format=custom",
            "--compress=6",
            "--file",
            str(temporary),
        ],
        text=True,
        capture_output=True,
        env=_pg_dump_environment(settings),
        timeout=1800,
        check=False,
    )
    if completed.returncode:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump dataset snapshot failed: {completed.stderr.strip()}")
    if not temporary.is_file() or temporary.stat().st_size < 1:
        raise RuntimeError("pg_dump produced no dataset snapshot")
    temporary.replace(destination)


def _content_identity(
    conn: Connection[dict[str, Any]],
) -> tuple[str, dict[str, int]]:
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        for relation, query in CONTENT_QUERIES:
            digest.update(relation.encode())
            digest.update(b"\0")
            count = 0
            with cur.copy(f"COPY ({query}) TO STDOUT WITH (FORMAT binary)") as copy:
                for chunk in copy:
                    digest.update(chunk)
            schema, table = relation.split(".", 1)
            cur.execute(f'SELECT count(*) AS count FROM "{schema}"."{table}"')
            count = int(cur.fetchone()["count"])  # type: ignore[index]
            counts[relation] = count
            digest.update(str(count).encode())
            digest.update(b"\0")
    return digest.hexdigest(), counts


def _create_consistent_snapshot(
    settings: Settings, destination: Path
) -> tuple[str, dict[str, int], str, str]:
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cur.execute("SELECT pg_export_snapshot() AS snapshot_id")
        snapshot_id = str(cur.fetchone()["snapshot_id"])  # type: ignore[index]
        content_sha256, counts = _content_identity(conn)
        schema_text, schema_sha256 = _run_schema_dump(settings, snapshot_id)
        _create_snapshot(settings, destination, snapshot_id)
        conn.commit()
    return content_sha256, counts, schema_text, schema_sha256


def _target_safety(
    settings: Settings,
    *,
    permitted_campaign_id: uuid.UUID | None = None,
    require_idle: bool = True,
) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) AS count FROM charm_control.campaigns
               WHERE status IN ('CREATED','RUNNING','PAUSED','STOPPING')
                 AND (%s::uuid IS NULL OR campaign_id<>%s)""",
            (permitted_campaign_id, permitted_campaign_id),
        )
        active_campaigns = int(cur.fetchone()["count"])  # type: ignore[index]
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version() AS version,current_database() AS database")
        identity = dict(cur.fetchone() or {})
        cur.execute("SELECT count(*) AS count FROM pg_settings WHERE pending_restart")
        pending_restart = int(cur.fetchone()["count"])  # type: ignore[index]
        cur.execute("SELECT count(*) AS count FROM pg_indexes WHERE indexname LIKE 'charm_idx_%'")
        managed_indexes = int(cur.fetchone()["count"])  # type: ignore[index]
        cur.execute(
            """SELECT count(*) AS count FROM pg_stat_activity
               WHERE application_name LIKE 'charmdb:%' AND pid<>pg_backend_pid()"""
        )
        active_sessions = int(cur.fetchone()["count"])  # type: ignore[index]
        relation_rows: dict[str, int] = {}
        for relation in PGBENCH_RELATIONS:
            schema, table = relation.split(".", 1)
            cur.execute(
                """SELECT count(*) AS count FROM pg_catalog.pg_class c
                   JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                   WHERE n.nspname=%s AND c.relname=%s""",
                (schema, table),
            )
            if int(cur.fetchone()["count"]) != 1:  # type: ignore[index]
                raise RuntimeError(f"required dataset relation {relation} is missing")
            cur.execute(f'SELECT count(*) AS count FROM "{schema}"."{table}"')
            relation_rows[relation] = int(cur.fetchone()["count"])  # type: ignore[index]
    safety = {
        **identity,
        "active_campaigns": active_campaigns,
        "pending_restart": pending_restart,
        "managed_indexes": managed_indexes,
        "active_charm_sessions": active_sessions,
        "relation_rows": relation_rows,
    }
    unsafe = {
        name: safety[name]
        for name in (
            "active_campaigns",
            "pending_restart",
            "managed_indexes",
            "active_charm_sessions",
        )
        if int(safety[name]) != 0
    }
    if unsafe and require_idle:
        raise RuntimeError(f"manifest preflight requires an idle safe target: {unsafe}")
    return safety


def _software_versions(settings: Settings) -> dict[str, str]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version() AS version")
        control = str(cur.fetchone()["version"])  # type: ignore[index]
    return {
        "charmdb": version("charm-db"),
        "charmdb_source_sha256": source_tree_sha256(),
        "python": platform.python_version(),
        "botorch": version("botorch"),
        "torch": version("torch"),
        "psycopg": version("psycopg"),
        "postgresql_control": control,
    }


def _f3_reference_candidates(settings: Settings) -> list[dict[str, Any]]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE((workflow_payload->>'warmup_seconds')::integer,0) AS warmup_seconds,
                   COALESCE((workflow_payload->>'duration_seconds')::integer,0) AS duration_seconds,
                   COALESCE((workflow_payload->>'concurrency')::integer,0) AS concurrency,
                   extract(epoch FROM (completed_at-COALESCE(started_at,created_at))) AS elapsed
            FROM charm_control.trials
            WHERE fidelity=3 AND completed_at IS NOT NULL
              AND state IN ('COMPLETED','SLO_VIOLATED','ROLLED_BACK')
              AND requested_configuration='{}'::jsonb
              AND workflow_payload ? 'duration_seconds'
            ORDER BY created_at
            """
        )
        rows = [dict(row) for row in cur.fetchall()]
    grouped: dict[tuple[int, int, int], list[float]] = {}
    for row in rows:
        key = (int(row["warmup_seconds"]), int(row["duration_seconds"]), int(row["concurrency"]))
        grouped.setdefault(key, []).append(float(row["elapsed"]))
    return [
        {
            "warmup_seconds": key[0],
            "measurement_seconds": key[1],
            "concurrency": key[2],
            "completed_trials": len(values),
            "median_trial_wall_clock_seconds": median(values),
            "minimum_trial_wall_clock_seconds": min(values),
            "maximum_trial_wall_clock_seconds": max(values),
        }
        for key, values in sorted(grouped.items())
    ]


def assess_manifest_evidence(
    reference_candidates: list[dict[str, Any]],
    warmup_seconds: int,
    measurement_seconds: int,
    concurrency: int,
) -> tuple[str, ...]:
    exact = [
        row
        for row in reference_candidates
        if row["warmup_seconds"] == warmup_seconds
        and row["measurement_seconds"] == measurement_seconds
        and row["concurrency"] == concurrency
        and row["completed_trials"] >= 5
    ]
    unresolved = ["logical dataset snapshot restore has not been validated"]
    if not exact:
        unresolved.append(
            "fewer than five default F3 references match the proposed "
            "warmup/measurement/concurrency"
        )
    return tuple(unresolved)


def run_manifest_preflight(
    settings: Settings,
    warmup_seconds: int = 120,
    measurement_seconds: int = 600,
    concurrency: int = 4,
) -> ManifestPreflightResult:
    if warmup_seconds < 0 or measurement_seconds < 1 or concurrency < 1:
        raise ValueError("invalid proposed F3 execution profile")
    settings.assert_target_allowed()
    captured_at = datetime.now(UTC)
    output_dir = (
        settings.artifact_dir / "dataset-snapshots" / captured_at.strftime("%Y%m%dT%H%M%S%fZ")
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    snapshot_path = output_dir / CANONICAL_SNAPSHOT_FILENAME
    evidence_path = output_dir / "manifest-evidence.json"
    safety = _target_safety(settings)
    resources = capture_and_validate_resources(settings)
    content_sha256, snapshot_rows, schema_text, schema_sha256 = _create_consistent_snapshot(
        settings, snapshot_path
    )
    schema_path = output_dir / "pgbench-schema.sql"
    schema_path.write_text(schema_text, encoding="utf-8", newline="\n")
    dataset_snapshot_sha256 = _sha256(snapshot_path)
    references = _f3_reference_candidates(settings)
    unresolved = assess_manifest_evidence(
        references, warmup_seconds, measurement_seconds, concurrency
    )
    measurement_gate = evaluate_prerequisite_gate(settings, "measurement_validity")
    knobs = {
        str(row["name"]): {
            "setting": row["setting"],
            "boot_val": row["boot_val"],
            "source": row["source"],
            "pending_restart": row["pending_restart"],
        }
        for row in discover_knobs(
            settings,
            {"random_page_cost", "work_mem", "effective_io_concurrency", "shared_buffers"},
        )
    }
    evidence = {
        "schema_version": 1,
        "captured_at": captured_at.isoformat(),
        "dataset": {
            "snapshot_sha256": dataset_snapshot_sha256,
            "snapshot_relative_path": str(snapshot_path.relative_to(settings.artifact_dir)),
            "snapshot_byte_size": snapshot_path.stat().st_size,
            "schema_sha256": schema_sha256,
            "schema_relative_path": str(schema_path.relative_to(settings.artifact_dir)),
            "content_sha256": content_sha256,
            "relations": list(PGBENCH_RELATIONS),
            "scale": snapshot_rows["public.pgbench_branches"],
            "relation_rows": snapshot_rows,
        },
        "proposed_execution_profile": {
            "warmup_seconds": warmup_seconds,
            "measurement_seconds": measurement_seconds,
            "concurrency": concurrency,
        },
        "target_safety": safety,
        "resources": resources,
        "software_versions": _software_versions(settings),
        "active_defaults": knobs,
        "measurement_gate": asdict(measurement_gate),
        "f3_reference_candidates": references,
        "cache_policy": FROZEN_CACHE_POLICY,
        "frozen_objective_reference_point": [0.0, -20.0],
        "practical_margins": FROZEN_PRACTICAL_MARGINS,
        "unresolved": list(unresolved),
        "eligible_to_freeze_real_manifest": not unresolved,
    }
    temporary = evidence_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    temporary.replace(evidence_path)
    evidence_sha256 = _sha256(evidence_path)
    preflight_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:manifest-preflight:{evidence_sha256}")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO charm_control.experiment_manifest_preflights
            (preflight_id,dataset_snapshot_sha256,schema_sha256,snapshot_relative_path,
             snapshot_byte_size,evidence_relative_path,evidence_sha256,evidence_byte_size,
             eligible,unresolved,evidence,dataset_content_sha256)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (evidence_sha256) DO NOTHING
            """,
            (
                preflight_id,
                dataset_snapshot_sha256,
                schema_sha256,
                str(snapshot_path.relative_to(settings.artifact_dir)),
                snapshot_path.stat().st_size,
                str(evidence_path.relative_to(settings.artifact_dir)),
                evidence_sha256,
                evidence_path.stat().st_size,
                not unresolved,
                Jsonb(list(unresolved)),
                Jsonb(evidence),
                content_sha256,
            ),
        )
        conn.commit()
    return ManifestPreflightResult(
        preflight_id,
        dataset_snapshot_sha256,
        schema_sha256,
        snapshot_path,
        evidence_path,
        evidence_sha256,
        not unresolved,
        unresolved,
    )


def _run_restore(settings: Settings, snapshot_path: Path) -> None:
    executable = shutil.which("pg_restore")
    if executable is None:
        raise RuntimeError("pg_restore is required for dataset restore validation")
    completed = subprocess.run(
        [
            executable,
            "--host",
            settings.target_host,
            "--port",
            str(settings.target_port),
            "--username",
            settings.target_user,
            "--dbname",
            settings.target_db,
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            "--single-transaction",
            str(snapshot_path),
        ],
        text=True,
        capture_output=True,
        env=_pg_dump_environment(settings),
        timeout=1800,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"pg_restore dataset validation failed: {completed.stderr.strip()}")


def validate_dataset_restore(
    settings: Settings,
    preflight_id: uuid.UUID,
    *,
    permitted_campaign_id: uuid.UUID | None = None,
) -> DatasetRestoreResult:
    _target_safety(settings, permitted_campaign_id=permitted_campaign_id)
    resources_before = capture_and_validate_resources(settings)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT dataset_snapshot_sha256,schema_sha256,snapshot_relative_path,
                      snapshot_byte_size,evidence,unresolved
               FROM charm_control.experiment_manifest_preflights WHERE preflight_id=%s""",
            (preflight_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown manifest preflight {preflight_id}")
    snapshot_path = (settings.artifact_dir / str(row["snapshot_relative_path"])).resolve()
    artifact_root = settings.artifact_dir.resolve()
    try:
        snapshot_path.relative_to(artifact_root)
    except ValueError as exc:
        raise ValueError("dataset snapshot path escapes the artifact directory") from exc
    if (
        not snapshot_path.is_file()
        or snapshot_path.stat().st_size != int(row["snapshot_byte_size"])
        or _sha256(snapshot_path) != str(row["dataset_snapshot_sha256"])
    ):
        raise ValueError("dataset snapshot artifact does not match persisted size/SHA-256")
    evidence = dict(row["evidence"])
    dataset = dict(evidence["dataset"])
    expected_content = str(dataset["content_sha256"])
    _run_restore(settings, snapshot_path)
    schema_text, observed_schema = _run_schema_dump(settings)
    del schema_text
    with connect(settings.target_dsn) as conn:
        observed_content, observed_rows = _content_identity(conn)
        conn.commit()
    resources_after = capture_and_validate_resources(settings)
    passed = (
        observed_schema == str(row["schema_sha256"])
        and observed_content == expected_content
        and observed_rows == dataset["relation_rows"]
    )
    details = {
        "snapshot_relative_path": row["snapshot_relative_path"],
        "snapshot_byte_size": row["snapshot_byte_size"],
        "observed_rows": observed_rows,
        "expected_rows": dataset["relation_rows"],
        "resources_before": resources_before,
        "resources_after": resources_after,
    }
    validation_id = uuid.uuid4()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO charm_control.experiment_dataset_restore_validations
            (validation_id,preflight_id,expected_snapshot_sha256,expected_schema_sha256,
             expected_content_sha256,observed_schema_sha256,observed_content_sha256,passed,details)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                validation_id,
                preflight_id,
                row["dataset_snapshot_sha256"],
                row["schema_sha256"],
                expected_content,
                observed_schema,
                observed_content,
                passed,
                Jsonb(details),
            ),
        )
        unresolved = tuple(str(item) for item in row["unresolved"])
        if passed:
            unresolved = tuple(
                item
                for item in unresolved
                if item != "logical dataset snapshot restore has not been validated"
            )
            cur.execute(
                """UPDATE charm_control.experiment_manifest_preflights
                   SET eligible=%s,unresolved=%s WHERE preflight_id=%s""",
                (not unresolved, Jsonb(list(unresolved)), preflight_id),
            )
        conn.commit()
    if not passed:
        raise RuntimeError("restored dataset does not match frozen schema/content evidence")
    return DatasetRestoreResult(
        validation_id,
        preflight_id,
        passed,
        observed_schema,
        observed_content,
        not unresolved,
        unresolved,
    )
