"""Neutral dataset fingerprint and restore-state operations."""

from __future__ import annotations

import math
import subprocess
import uuid
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from psycopg import sql

from charmdb.config import Settings
from charmdb.controller import ALLOWED_KNOBS
from charmdb.db import connect
from charmdb.restore.models import FingerprintComparison

PGBENCH_RELATIONS = (
    "public.pgbench_accounts",
    "public.pgbench_branches",
    "public.pgbench_history",
    "public.pgbench_tellers",
)


DEFAULT_PHYSICAL_TOLERANCES: dict[str, dict[str, float]] = {
    "database_size_bytes": {"relative": 0.02, "absolute": 1024 * 1024},
    "relation_sizes.*.*": {"relative": 0.02, "absolute": 1024 * 1024},
    "statistics.*.n_live_tup": {"relative": 0.05, "absolute": 10.0},
    "statistics.*.n_dead_tup": {"relative": 0.05, "absolute": 10.0},
    "statistics.*.n_mod_since_analyze": {"relative": 0.05, "absolute": 10.0},
    "statistics.*.vacuum_count": {"relative": 0.0, "absolute": 1.0},
    "statistics.*.analyze_count": {"relative": 0.0, "absolute": 1.0},
    "statistics.*.autovacuum_count": {"relative": 0.0, "absolute": 1.0},
    "statistics.*.autoanalyze_count": {"relative": 0.0, "absolute": 1.0},
    "checkpoint_wal.wal_flush_lag_bytes": {
        "relative": 0.0,
        "absolute": 16 * 1024 * 1024,
    },
}


CANONICAL_SETTING_NAMES = ALLOWED_KNOBS | {
    "autovacuum",
    "default_transaction_isolation",
    "track_counts",
}


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in sorted(value.items()):
            child = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten(item, child))
        return result
    return {prefix: value}


def _tolerance_for(path: str, tolerances: dict[str, dict[str, float]]) -> dict[str, float] | None:
    if path in tolerances:
        return tolerances[path]
    for pattern, tolerance in tolerances.items():
        if fnmatchcase(path, pattern):
            return tolerance
    return None


def compare_fingerprints(
    expected_exact: dict[str, Any],
    expected_physical: dict[str, Any],
    observed_exact: dict[str, Any],
    observed_physical: dict[str, Any],
    tolerances: dict[str, dict[str, float]],
) -> FingerprintComparison:
    expected_exact_flat = _flatten(expected_exact)
    observed_exact_flat = _flatten(observed_exact)
    exact_mismatches: dict[str, dict[str, Any]] = {}
    for path in sorted(set(expected_exact_flat) | set(observed_exact_flat)):
        expected = expected_exact_flat.get(path)
        observed = observed_exact_flat.get(path)
        if expected != observed:
            exact_mismatches[path] = {"expected": expected, "observed": observed}

    expected_physical_flat = _flatten(expected_physical)
    observed_physical_flat = _flatten(observed_physical)
    physical_mismatches: dict[str, dict[str, Any]] = {}
    for path in sorted(set(expected_physical_flat) | set(observed_physical_flat)):
        expected = expected_physical_flat.get(path)
        observed = observed_physical_flat.get(path)
        tolerance = _tolerance_for(path, tolerances)
        if (
            tolerance is None
            or not isinstance(expected, (int, float))
            or not isinstance(observed, (int, float))
        ):
            if expected != observed:
                physical_mismatches[path] = {
                    "expected": expected,
                    "observed": observed,
                    "reason": "missing tolerance or non-numeric value",
                }
            continue
        absolute = float(tolerance.get("absolute", 0.0))
        relative = float(tolerance.get("relative", 0.0))
        allowed = max(absolute, abs(float(expected)) * relative)
        difference = abs(float(observed) - float(expected))
        if not math.isfinite(difference) or difference > allowed:
            physical_mismatches[path] = {
                "expected": expected,
                "observed": observed,
                "difference": difference,
                "allowed": allowed,
            }
    return FingerprintComparison(
        not exact_mismatches,
        not physical_mismatches,
        exact_mismatches,
        physical_mismatches,
    )


def _target_image_digest() -> str:
    container = subprocess.run(
        ["docker", "compose", "ps", "-q", "target-postgres"],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    container_id = container.stdout.strip().splitlines()
    if container.returncode != 0 or not container_id:
        raise RuntimeError("target container is not available for image verification")
    inspect = subprocess.run(
        ["docker", "inspect", container_id[0], "--format", "{{.Image}}"],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if inspect.returncode != 0 or not inspect.stdout.strip():
        raise RuntimeError("target image digest could not be inspected")
    return inspect.stdout.strip()


def _load_preflight(settings: Settings, preflight_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT dataset_snapshot_sha256,schema_sha256,evidence
               FROM charm_control.experiment_manifest_preflights WHERE preflight_id=%s""",
            (preflight_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown manifest preflight {preflight_id}")
    evidence = dict(row["evidence"])
    return {
        "snapshot_sha256": str(row["dataset_snapshot_sha256"]),
        "schema_sha256": str(row["schema_sha256"]),
        "evidence": evidence,
    }


def _load_validation(
    settings: Settings, validation_id: uuid.UUID, preflight_id: uuid.UUID
) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT validation_id,preflight_id,expected_snapshot_sha256,
                      observed_schema_sha256,observed_content_sha256,passed,details
               FROM charm_control.experiment_dataset_restore_validations
               WHERE validation_id=%s""",
            (validation_id,),
        )
        row = cur.fetchone()
    if row is None or row["preflight_id"] != preflight_id or not bool(row["passed"]):
        raise ValueError("candidate baseline requires a matching passed restore validation")
    return dict(row)


def capture_live_fingerprint(
    settings: Settings,
    preflight_id: uuid.UUID,
    validation_id: uuid.UUID,
) -> tuple[dict[str, Any], dict[str, Any]]:
    preflight = _load_preflight(settings, preflight_id)
    validation = _load_validation(settings, validation_id, preflight_id)
    dataset = dict(preflight["evidence"]["dataset"])
    active_defaults = dict(preflight["evidence"].get("active_defaults") or {})
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name,setting FROM pg_settings WHERE name = ANY(%s) ORDER BY name",
            (list(CANONICAL_SETTING_NAMES),),
        )
        active_settings = {str(row["name"]): str(row["setting"]) for row in cur.fetchall()}
    if set(active_settings) != CANONICAL_SETTING_NAMES:
        missing = sorted(CANONICAL_SETTING_NAMES - set(active_settings))
        raise RuntimeError(f"target does not expose canonical settings: {missing}")
    expected_settings = {
        str(name): str(dict(definition).get("setting"))
        for name, definition in active_defaults.items()
    }
    observed_preflight_settings = {name: active_settings.get(name) for name in expected_settings}
    if observed_preflight_settings != expected_settings:
        raise RuntimeError("live settings differ from the canonical preflight settings")
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        relation_rows: dict[str, int] = {}
        relation_sizes: dict[str, dict[str, int]] = {}
        for relation in PGBENCH_RELATIONS:
            schema_name, relation_name = relation.split(".", 1)
            cur.execute(
                sql.SQL("SELECT count(*)::bigint AS count FROM {}.{}").format(
                    sql.Identifier(schema_name), sql.Identifier(relation_name)
                )
            )
            relation_rows[relation] = int(cur.fetchone()["count"])  # type: ignore[index]
            cur.execute(
                """SELECT pg_relation_size(%s)::bigint AS heap_bytes,
                          pg_indexes_size(%s)::bigint AS index_bytes,
                          (pg_total_relation_size(%s)-pg_relation_size(%s)-
                           pg_indexes_size(%s))::bigint AS toast_bytes,
                          pg_total_relation_size(%s)::bigint AS total_bytes""",
                (relation, relation, relation, relation, relation, relation),
            )
            size_row = cur.fetchone()
            if size_row is None:
                raise RuntimeError(f"size query returned no row for {relation}")
            relation_sizes[relation] = {
                "heap_bytes": int(size_row["heap_bytes"]),
                "index_bytes": int(size_row["index_bytes"]),
                "toast_bytes": int(size_row["toast_bytes"]),
                "total_bytes": int(size_row["total_bytes"]),
            }
        cur.execute(
            """SELECT current_database() AS database,
                      current_setting('server_version') AS postgres_version,
                      pg_database_size(current_database())::bigint AS database_size_bytes"""
        )
        identity = dict(cur.fetchone() or {})
        cur.execute(
            """SELECT schemaname||'.'||relname AS relation,
                      n_live_tup::bigint,n_dead_tup::bigint,n_mod_since_analyze::bigint,
                      vacuum_count::bigint,analyze_count::bigint,
                      autovacuum_count::bigint,autoanalyze_count::bigint,
                      last_vacuum IS NOT NULL AS vacuum_observed,
                      last_analyze IS NOT NULL AS analyze_observed
               FROM pg_stat_all_tables
               WHERE schemaname='public' AND relname = ANY(%s)
               ORDER BY relname""",
            ([item.split(".", 1)[1] for item in PGBENCH_RELATIONS],),
        )
        statistics = {
            str(row["relation"]): {
                "n_live_tup": int(row["n_live_tup"]),
                "n_dead_tup": int(row["n_dead_tup"]),
                "n_mod_since_analyze": int(row["n_mod_since_analyze"]),
                "vacuum_count": int(row["vacuum_count"]),
                "analyze_count": int(row["analyze_count"]),
                "autovacuum_count": int(row["autovacuum_count"]),
                "autoanalyze_count": int(row["autoanalyze_count"]),
                "vacuum_observed": bool(row["vacuum_observed"]),
                "analyze_observed": bool(row["analyze_observed"]),
            }
            for row in cur.fetchall()
        }
        cur.execute(
            """SELECT schemaname||'.'||sequencename AS sequence,
                      start_value,increment_by,min_value,max_value,cache_size,cycle,last_value
               FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename"""
        )
        sequences = {
            str(row["sequence"]): {
                key: row[key]
                for key in (
                    "start_value",
                    "increment_by",
                    "min_value",
                    "max_value",
                    "cache_size",
                    "cycle",
                    "last_value",
                )
            }
            for row in cur.fetchall()
        }
        cur.execute(
            """SELECT pg_wal_lsn_diff(
                          pg_current_wal_lsn(),pg_current_wal_flush_lsn()
                      )::bigint AS wal_flush_lag_bytes"""
        )
        checkpoint_wal = dict(cur.fetchone() or {})
    exact_core = {
        "snapshot_sha256": preflight["snapshot_sha256"],
        "schema_sha256": str(validation["observed_schema_sha256"]),
        "content_sha256": str(validation["observed_content_sha256"]),
        "relation_rows": relation_rows,
        "pgbench_history_rows": relation_rows.get("public.pgbench_history", 0),
        "sequences": sequences,
        "active_settings": active_settings,
        "database": str(identity["database"]),
        "postgres_version": str(identity["postgres_version"]),
        "image_digest": _target_image_digest(),
    }
    expected_rows = {str(key): int(value) for key, value in dataset["relation_rows"].items()}
    if relation_rows != expected_rows:
        raise RuntimeError("live relation rows differ from the preflight dataset evidence")
    physical_statistics = {
        "database_size_bytes": int(identity["database_size_bytes"]),
        "relation_sizes": relation_sizes,
        "statistics": statistics,
        "checkpoint_wal": {
            "wal_flush_lag_bytes": int(checkpoint_wal["wal_flush_lag_bytes"]),
        },
    }
    return exact_core, physical_statistics


def standardize_logical_restore_state(settings: Settings) -> None:
    """Make planner statistics and the checkpoint boundary part of the restore contract."""
    settings.assert_target_allowed()
    with connect(settings.target_dsn) as conn:
        conn.autocommit = True
        cur = conn.cursor()
        for relation in PGBENCH_RELATIONS:
            schema_name, relation_name = relation.split(".", 1)
            cur.execute(
                sql.SQL("VACUUM (ANALYZE) {}.{}").format(
                    sql.Identifier(schema_name), sql.Identifier(relation_name)
                )
            )
        cur.execute("CHECKPOINT")
