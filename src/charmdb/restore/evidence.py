"""Assess historical F3 measurement evidence without experiment-arm orchestration."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from charmdb.config import Settings
from charmdb.db import connect


@dataclass(frozen=True)
class MeasurementEvidenceAssessment:
    gate: str
    eligible: bool
    reasons: tuple[str, ...]
    evidence: dict[str, Any]


def assess_f3_measurement_evidence(settings: Settings) -> MeasurementEvidenceAssessment:
    """Preserve the original preflight evidence contract for existing snapshots."""
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.trial_id,t.requested_configuration,t.active_configuration,
                   a.relative_path,a.sha256,a.byte_size
            FROM charm_control.trials t
            LEFT JOIN charm_control.artifacts a USING(trial_id)
            WHERE t.fidelity=3 AND t.completed_at IS NOT NULL
              AND t.state = ANY(%s)
              AND COALESCE(t.objective_values->>'throughput_tps',
                           t.workflow_result->>'throughput_tps') IS NOT NULL
              AND COALESCE(t.objective_values->>'p99_ms',
                           t.workflow_result->>'p99_ms') IS NOT NULL
            """,
            (["COMPLETED", "SLO_VIOLATED", "ROLLED_BACK"],),
        )
        rows = [dict(row) for row in cur.fetchall()]

    measured_ids = {str(row["trial_id"]) for row in rows}
    matched_ids = {
        str(row["trial_id"])
        for row in rows
        if all(
            str(row["active_configuration"].get(name)) == str(value)
            for name, value in row["requested_configuration"].items()
        )
    }
    artifact_checks: dict[str, list[bool]] = {}
    mismatches = 0
    artifact_root = settings.artifact_dir.resolve()
    for row in rows:
        if row["relative_path"] is None:
            continue
        path = (settings.artifact_dir / str(row["relative_path"])).resolve()
        try:
            path.relative_to(artifact_root)
        except ValueError:
            artifact_checks.setdefault(str(row["trial_id"]), []).append(False)
            mismatches += 1
            continue
        valid = (
            path.is_file()
            and path.stat().st_size == int(row["byte_size"])
            and hashlib.sha256(path.read_bytes()).hexdigest() == str(row["sha256"])
        )
        artifact_checks.setdefault(str(row["trial_id"]), []).append(valid)
        if not valid:
            mismatches += 1
    verified_ids = {
        trial_id for trial_id, checks in artifact_checks.items() if checks and all(checks)
    }
    evidence = {
        "measured_f3_trials": len(measured_ids),
        "active_configuration_matched_f3_trials": len(matched_ids),
        "artifact_backed_f3_trials": len(verified_ids),
        "artifact_hash_mismatches": mismatches,
    }
    reasons = []
    if len(measured_ids) < 5:
        reasons.append("fewer than five completed F3 measurements")
    if len(matched_ids) < 5:
        reasons.append("fewer than five F3 trials matched requested and active settings")
    if len(verified_ids) < 5:
        reasons.append("fewer than five F3 measurements have hash-verified raw artifacts")
    return MeasurementEvidenceAssessment(
        "measurement_validity", not reasons, tuple(reasons), evidence
    )
