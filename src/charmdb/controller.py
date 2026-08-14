from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.db import connect

ALLOWED_KNOBS = frozenset(
    {
        "shared_buffers",
        "effective_cache_size",
        "work_mem",
        "maintenance_work_mem",
        "wal_buffers",
        "checkpoint_timeout",
        "checkpoint_completion_target",
        "max_wal_size",
        "random_page_cost",
        "effective_io_concurrency",
        "default_statistics_target",
        "max_parallel_workers_per_gather",
    }
)

# Native pg_settings units. Bounds include the verified target defaults so rollback is always legal.
RESEARCH_NUMERIC_BOUNDS: dict[str, tuple[Decimal, Decimal]] = {
    "shared_buffers": (Decimal("16384"), Decimal("183500")),  # 128 MiB to ~1.4 GiB
    "effective_cache_size": (Decimal("131072"), Decimal("524288")),
    "work_mem": (Decimal("1024"), Decimal("32768")),
    "maintenance_work_mem": (Decimal("32768"), Decimal("524288")),
    "checkpoint_timeout": (Decimal("300"), Decimal("1800")),
    "checkpoint_completion_target": (Decimal("0.7"), Decimal("0.95")),
    "max_wal_size": (Decimal("512"), Decimal("4096")),
    "random_page_cost": (Decimal("1"), Decimal("4")),
    "effective_io_concurrency": (Decimal("0"), Decimal("200")),
    "default_statistics_target": (Decimal("100"), Decimal("500")),
    "max_parallel_workers_per_gather": (Decimal("0"), Decimal("4")),
}


@dataclass(frozen=True)
class ApplyResult:
    application_id: uuid.UUID
    snapshot_id: uuid.UUID
    requested: dict[str, str]
    previous: dict[str, str]
    verified: dict[str, str]
    requires_restart: bool
    duration_seconds: float


def discover_knobs(settings: Settings, names: set[str] | None = None) -> list[dict[str, Any]]:
    selected = names or set(ALLOWED_KNOBS)
    unknown = selected - ALLOWED_KNOBS
    if unknown:
        raise ValueError(f"knobs outside the bounded allowlist: {sorted(unknown)}")
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT name, setting, unit, category, short_desc, context, vartype,
                      min_val, max_val, enumvals, boot_val, reset_val, source,
                      pending_restart
               FROM pg_settings WHERE name = ANY(%s) ORDER BY name""",
            (list(selected),),
        )
        rows = [dict(row) for row in cur.fetchall()]
    found = {row["name"] for row in rows}
    if missing := selected - found:
        raise ValueError(f"target does not expose expected knobs: {sorted(missing)}")
    return rows


def validate_candidate(candidate: Mapping[str, str], metadata: list[dict[str, Any]]) -> None:
    if not candidate:
        raise ValueError("candidate must contain at least one knob")
    by_name = {row["name"]: row for row in metadata}
    for name, value in candidate.items():
        if name not in ALLOWED_KNOBS or name not in by_name:
            raise ValueError(f"knob {name!r} is not in the bounded runtime allowlist")
        row = by_name[name]
        if row["context"] in {"internal"}:
            raise ValueError(f"knob {name!r} is not mutable")
        if row["vartype"] in {"integer", "real"}:
            try:
                numeric = Decimal(value)
            except InvalidOperation as error:
                raise ValueError(
                    f"{name} requires a value in native unit {row['unit']!r}"
                ) from error
            minimum = Decimal(row["min_val"]) if row["min_val"] is not None else None
            maximum = Decimal(row["max_val"]) if row["max_val"] is not None else None
            if minimum is not None and numeric < minimum:
                raise ValueError(f"{name}={value} is below runtime minimum {minimum}")
            if maximum is not None and numeric > maximum:
                raise ValueError(f"{name}={value} is above runtime maximum {maximum}")
            if name in RESEARCH_NUMERIC_BOUNDS:
                research_minimum, research_maximum = RESEARCH_NUMERIC_BOUNDS[name]
                if numeric < research_minimum:
                    raise ValueError(f"{name}={value} is below research minimum {research_minimum}")
                if numeric > research_maximum:
                    raise ValueError(f"{name}={value} is above research maximum {research_maximum}")
            if name == "wal_buffers" and numeric not in {
                Decimal("-1"),
                Decimal("512"),
                Decimal("1024"),
                Decimal("2048"),
            }:
                raise ValueError("wal_buffers must be auto/-1, 4, 8, or 16 MiB in native blocks")
        elif row["vartype"] == "bool" and value.lower() not in {"on", "off", "true", "false"}:
            raise ValueError(f"{name} requires a Boolean value")
        elif row["vartype"] == "enum" and value not in (row["enumvals"] or []):
            raise ValueError(f"{name} requires one of {row['enumvals']}")


def _restart_target(timeout: int = 90) -> None:
    completed = subprocess.run(
        ["docker", "compose", "restart", "target-postgres"],
        cwd=Path.cwd(),
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"target restart failed: {completed.stderr.strip()}")


def _wait_ready(settings: Settings, timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with connect(settings.target_dsn) as conn, conn.cursor() as cur:
                cur.execute("SELECT NOT pg_is_in_recovery() AS ready")
                row = cur.fetchone()
                if row and row["ready"]:
                    return
        except Exception as error:
            last_error = error
        time.sleep(1)
    raise TimeoutError(f"target did not become ready: {last_error}")


def _set_system(settings: Settings, values: Mapping[str, str]) -> None:
    with connect(settings.target_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for name, value in values.items():
                cur.execute(
                    sql.SQL("ALTER SYSTEM SET {} TO {}").format(
                        sql.Identifier(name), sql.Literal(value)
                    )
                )


def _activate(settings: Settings, restart: bool, restart_fn: Callable[[], None]) -> None:
    if restart:
        restart_fn()
        _wait_ready(settings)
    else:
        with connect(settings.target_dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_reload_conf() AS reloaded")
            row = cur.fetchone()
            if not row or not row["reloaded"]:
                raise RuntimeError("PostgreSQL rejected configuration reload")
        time.sleep(1)


def _read_active(settings: Settings, names: set[str]) -> dict[str, str]:
    rows = discover_knobs(settings, names)
    return {row["name"]: row["setting"] for row in rows}


def apply_configuration(
    settings: Settings,
    candidate: Mapping[str, str],
    restart_fn: Callable[[], None] | None = None,
    application_id: uuid.UUID | None = None,
    snapshot_id: uuid.UUID | None = None,
    force_restart: bool = False,
) -> ApplyResult:
    restart_fn = restart_fn or _restart_target
    settings.assert_target_allowed()
    requested = {name: str(value) for name, value in candidate.items()}
    metadata = discover_knobs(settings, set(requested))
    validate_candidate(requested, metadata)
    discovered_previous = {row["name"]: row["setting"] for row in metadata}
    discovered_restart = any(row["context"] == "postmaster" for row in metadata)
    application_id = application_id or uuid.uuid4()
    snapshot_id = snapshot_id or uuid.uuid4()

    with connect(settings.control_dsn) as control, control.cursor() as cur:
        cur.execute(
            """SELECT a.snapshot_id,a.requested_settings,a.verified_settings,a.status,
                      a.requires_restart,a.apply_duration_seconds,s.settings AS previous
               FROM charm_control.configuration_applications a
               JOIN charm_control.configuration_snapshots s USING(snapshot_id)
               WHERE a.application_id=%s""",
            (application_id,),
        )
        existing = cur.fetchone()
    if existing is not None:
        stored_requested = {
            str(name): str(value) for name, value in existing["requested_settings"].items()
        }
        if stored_requested != requested:
            raise ValueError("application id is already bound to a different configuration")
        snapshot_id = existing["snapshot_id"]
        previous = {str(name): str(value) for name, value in existing["previous"].items()}
        requires_restart = bool(existing["requires_restart"])
        active = _read_active(settings, set(requested))
        if active == requested:
            duration = float(existing["apply_duration_seconds"] or 0.0)
            with connect(settings.control_dsn) as control, control.cursor() as cur:
                cur.execute(
                    """UPDATE charm_control.configuration_applications
                    SET verified_settings=%s,status='VERIFIED',completed_at=clock_timestamp()
                    WHERE application_id=%s""",
                    (Jsonb(active), application_id),
                )
                control.commit()
            return ApplyResult(
                application_id,
                snapshot_id,
                requested,
                previous,
                active,
                requires_restart,
                duration,
            )
        if active != previous:
            rollback_configuration(
                settings,
                application_id,
                "reconcile ambiguous configuration before retry",
                restart_fn,
            )
        with connect(settings.control_dsn) as control, control.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.configuration_applications
                SET status='APPLYING',failure_details='{}'::jsonb,completed_at=NULL
                WHERE application_id=%s""",
                (application_id,),
            )
            control.commit()
    else:
        previous = discovered_previous
        requires_restart = discovered_restart

        with connect(settings.target_dsn) as target, target.cursor() as cur:
            cur.execute("SELECT current_setting('server_version') AS version")
            server_version = cur.fetchone()["version"]  # type: ignore[index]
        with connect(settings.control_dsn) as control, control.cursor() as cur:
            cur.execute(
                """INSERT INTO charm_control.configuration_snapshots
                (snapshot_id, label, postgres_version, settings) VALUES (%s, %s, %s, %s)""",
                (snapshot_id, "last-known-good", server_version, Jsonb(previous)),
            )
            cur.execute(
                """INSERT INTO charm_control.configuration_applications
                (application_id, snapshot_id, requested_settings, status, requires_restart)
                VALUES (%s, %s, %s, 'APPLYING', %s)""",
                (application_id, snapshot_id, Jsonb(requested), requires_restart),
            )
            control.commit()

    started = time.monotonic()
    try:
        _set_system(settings, requested)
        _activate(settings, requires_restart or force_restart, restart_fn)
        verified = _read_active(settings, set(requested))
        if verified != requested:
            raise RuntimeError(f"active settings differ: requested={requested}, active={verified}")
        duration = time.monotonic() - started
        with connect(settings.control_dsn) as control, control.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.configuration_applications
                SET verified_settings=%s, status='VERIFIED', apply_duration_seconds=%s,
                    completed_at=clock_timestamp() WHERE application_id=%s""",
                (Jsonb(verified), duration, application_id),
            )
            control.commit()
        return ApplyResult(
            application_id, snapshot_id, requested, previous, verified, requires_restart, duration
        )
    except Exception as error:
        duration = time.monotonic() - started
        with connect(settings.control_dsn) as control, control.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.configuration_applications
                SET status='FAILED', failure_details=%s, apply_duration_seconds=%s,
                    completed_at=clock_timestamp() WHERE application_id=%s""",
                (Jsonb({"error": str(error)}), duration, application_id),
            )
            control.commit()
        try:
            rollback_configuration(
                settings,
                application_id,
                f"automatic rollback after apply failure: {error}",
                restart_fn,
            )
        except Exception as rollback_error:
            with connect(settings.control_dsn) as control, control.cursor() as cur:
                cur.execute(
                    """UPDATE charm_control.configuration_applications
                    SET failure_details=%s WHERE application_id=%s""",
                    (
                        Jsonb({"error": str(error), "rollback_error": str(rollback_error)}),
                        application_id,
                    ),
                )
                control.commit()
        raise


def rollback_configuration(
    settings: Settings,
    application_id: uuid.UUID,
    reason: str,
    restart_fn: Callable[[], None] | None = None,
) -> dict[str, str]:
    restart_fn = restart_fn or _restart_target
    with connect(settings.control_dsn) as control, control.cursor() as cur:
        cur.execute(
            """SELECT a.requires_restart, s.settings
            FROM charm_control.configuration_applications a
            JOIN charm_control.configuration_snapshots s USING(snapshot_id)
            WHERE a.application_id=%s""",
            (application_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"unknown application {application_id}")
        previous = {str(key): str(value) for key, value in row["settings"].items()}
        requires_restart = bool(row["requires_restart"])

        cur.execute(
            """SELECT restored_settings,verified FROM charm_control.rollbacks
            WHERE application_id=%s""",
            (application_id,),
        )
        existing_rollback = cur.fetchone()

    if existing_rollback is not None and existing_rollback["verified"]:
        restored = {
            str(key): str(value) for key, value in existing_rollback["restored_settings"].items()
        }
        active = _read_active(settings, set(restored))
        if active == restored:
            return active

    started = time.monotonic()
    _set_system(settings, previous)
    _activate(settings, requires_restart, restart_fn)
    verified = _read_active(settings, set(previous))
    if verified != previous:
        raise RuntimeError(f"rollback verification failed: expected={previous}, active={verified}")
    duration = time.monotonic() - started
    with connect(settings.control_dsn) as control, control.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.rollbacks
            (rollback_id, application_id, reason, restored_settings, verified, duration_seconds)
            VALUES (%s, %s, %s, %s, true, %s)
            ON CONFLICT (application_id) DO UPDATE
            SET reason=EXCLUDED.reason,restored_settings=EXCLUDED.restored_settings,
                verified=true,duration_seconds=EXCLUDED.duration_seconds,
                created_at=clock_timestamp()""",
            (uuid.uuid4(), application_id, reason, Jsonb(verified), duration),
        )
        cur.execute(
            """UPDATE charm_control.configuration_applications SET status='ROLLED_BACK'
            WHERE application_id=%s""",
            (application_id,),
        )
        control.commit()
    return verified


def result_json(result: ApplyResult) -> str:
    return json.dumps(
        {
            "application_id": str(result.application_id),
            "snapshot_id": str(result.snapshot_id),
            "requested": result.requested,
            "previous": result.previous,
            "verified": result.verified,
            "requires_restart": result.requires_restart,
            "duration_seconds": result.duration_seconds,
        },
        indent=2,
    )
