from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.db import connect

ANALYSIS_METHODS = {
    "independent_unit": "seeded campaign or independently restored benchmark block",
    "descriptive": ["raw", "n", "mean", "median", "SD", "MAD", "95% bootstrap CI"],
    "paired": ["paired permutation", "standardized paired effect", "Cliff's delta"],
    "multiple_comparisons": "Holm adjustment for preregistered confirmatory families",
    "prohibited": "transaction rows are not independent replicates",
}


@dataclass(frozen=True)
class AnalysisReadiness:
    status: str
    registered_groups: int
    registered_arms: int
    completed_arms: int
    missing_arms: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class AnalysisReadinessResult:
    analysis_report_id: uuid.UUID
    output_path: Path
    sha256: str
    readiness: AnalysisReadiness


def _registry_complete(
    group_count: int,
    completed_group_count: int,
    arm_count: int,
    completed_arm_count: int,
    missing_arm_count: int,
) -> bool:
    return (
        group_count == 19
        and completed_group_count == 19
        and arm_count == 168
        and completed_arm_count == 168
        and missing_arm_count == 0
    )


def analysis_readiness(settings: Settings) -> AnalysisReadiness:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) AS count,
                      count(*) FILTER (WHERE status='COMPLETED') AS completed
               FROM charm_control.experiment_groups"""
        )
        group_row = cur.fetchone()
        if group_row is None:
            raise RuntimeError("experiment group count query returned no row")
        group_count = int(group_row["count"])
        completed_group_count = int(group_row["completed"])
        cur.execute(
            """SELECT count(*) AS count,
                      count(*) FILTER (WHERE status='COMPLETED') AS completed
               FROM charm_control.experiment_arms"""
        )
        arm_row = cur.fetchone()
        if arm_row is None:
            raise RuntimeError("experiment arm count query returned no row")
        arm_count = int(arm_row["count"])
        completed_arm_count = int(arm_row["completed"])
        cur.execute(
            """
            SELECT g.group_key,g.status AS group_status,a.arm_id,a.label,a.random_seed,a.status,
                   a.campaign_id,c.status AS campaign_status,a.manifest_sha256,
                   a.budget_value,a.realized_budget_value
            FROM charm_control.experiment_arms a
            JOIN charm_control.experiment_groups g USING(group_id)
            LEFT JOIN charm_control.campaigns c USING(campaign_id)
            WHERE a.status <> 'COMPLETED'
               OR a.campaign_id IS NULL OR c.status <> 'COMPLETED'
               OR a.execution_manifest IS NULL OR a.manifest_sha256 IS NULL
               OR a.realized_budget_value < a.budget_value
               OR a.realized_budget_value > a.budget_value * (
                   1 + COALESCE(
                       (a.execution_manifest #>>
                           '{execution,budget_accounting,relative_tolerance}')::double precision,
                       -1
                   )
               )
            ORDER BY g.group_key,a.random_seed,a.block_order
            """
        )
        missing = tuple(dict(row) for row in cur.fetchall())
    complete = _registry_complete(
        group_count,
        completed_group_count,
        arm_count,
        completed_arm_count,
        len(missing),
    )
    return AnalysisReadiness(
        "COMPLETE" if complete else "INCOMPLETE",
        group_count,
        arm_count,
        completed_arm_count,
        missing,
    )


def generate_analysis_readiness(
    settings: Settings, output_root: Path | None = None
) -> AnalysisReadinessResult:
    readiness = analysis_readiness(settings)
    generated_at = datetime.now(UTC)
    root = output_root or settings.artifact_dir / "analysis"
    artifact_root = settings.artifact_dir.resolve()
    try:
        root.resolve().relative_to(artifact_root)
    except ValueError as exc:
        raise ValueError("analysis output must be below the configured artifact directory") from exc
    output_dir = root / generated_at.strftime("%Y%m%dT%H%M%S%fZ")
    output_dir.mkdir(parents=True, exist_ok=False)
    output_path = output_dir / "readiness.json"
    payload = {
        "generated_at": generated_at.isoformat(),
        "readiness": asdict(readiness),
        "methods": ANALYSIS_METHODS,
        "interpretation": (
            "No comparative effectiveness analysis is permitted while status is INCOMPLETE."
        ),
    }
    output_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    analysis_report_id = uuid.uuid4()
    relative_path = output_path.resolve().relative_to(artifact_root)
    missing_arms_json = json.loads(json.dumps(readiness.missing_arms, default=str))
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO charm_control.analysis_reports
            (analysis_report_id,status,independent_unit,registered_groups,registered_arms,
             completed_arms,missing_arms,methods,relative_path,sha256,byte_size)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                analysis_report_id,
                readiness.status,
                ANALYSIS_METHODS["independent_unit"],
                readiness.registered_groups,
                readiness.registered_arms,
                readiness.completed_arms,
                Jsonb(missing_arms_json),
                Jsonb(ANALYSIS_METHODS),
                str(relative_path),
                digest,
                output_path.stat().st_size,
            ),
        )
        conn.commit()
    return AnalysisReadinessResult(analysis_report_id, output_path, digest, readiness)
