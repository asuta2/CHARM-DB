from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.db import connect


@dataclass(frozen=True)
class OptimizerGateDecision:
    decision: str
    probabilistic_gate_enabled: bool
    early_stopping_enabled: bool
    reasons: tuple[str, ...]


def decide_optimizer_gate(
    probabilistic_gate_enabled: bool,
    infeasible_labels: int,
    early_stopping_enabled: bool,
) -> OptimizerGateDecision:
    reasons: list[str] = []
    calibrated = probabilistic_gate_enabled and infeasible_labels > 0
    if not probabilistic_gate_enabled:
        reasons.append("latest calibration activation gate is closed")
    elif infeasible_labels <= 0:
        reasons.append("calibration evidence contains no infeasible labels")
    if not early_stopping_enabled:
        reasons.append("latest paired-fidelity early-stopping gate is closed")
    adaptive = calibrated and early_stopping_enabled
    if not adaptive:
        reasons.append("full F3 evaluation remains mandatory")
    return OptimizerGateDecision(
        "ADAPTIVE_ELIGIBLE" if adaptive else "FULL_F3_REQUIRED",
        calibrated,
        early_stopping_enabled,
        tuple(reasons),
    )


def persist_optimizer_gate_decision(
    settings: Settings, recommendation_id: uuid.UUID
) -> OptimizerGateDecision:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT report_id,infeasible_labels,probabilistic_promotion_enabled
            FROM charm_control.calibration_reports ORDER BY created_at DESC LIMIT 1
            """
        )
        calibration = cur.fetchone()
        cur.execute(
            """
            SELECT report_id,early_stopping_enabled
            FROM charm_control.fidelity_reports ORDER BY created_at DESC LIMIT 1
            """
        )
        fidelity = cur.fetchone()
        decision = decide_optimizer_gate(
            bool(calibration and calibration["probabilistic_promotion_enabled"]),
            int(calibration["infeasible_labels"]) if calibration else 0,
            bool(fidelity and fidelity["early_stopping_enabled"]),
        )
        cur.execute(
            """
            INSERT INTO charm_control.optimizer_gate_decisions
            (decision_id,recommendation_id,calibration_report_id,fidelity_report_id,
             probabilistic_gate_enabled,early_stopping_enabled,decision,reasons)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (recommendation_id) DO NOTHING
            """,
            (
                uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{recommendation_id}:gate"),
                recommendation_id,
                calibration["report_id"] if calibration else None,
                fidelity["report_id"] if fidelity else None,
                decision.probabilistic_gate_enabled,
                decision.early_stopping_enabled,
                decision.decision,
                Jsonb(decision.reasons),
            ),
        )
        conn.commit()
    return decision


def optimizer_gate_status(settings: Settings) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT report_id,infeasible_labels,probabilistic_promotion_enabled,activation_gate,
                   created_at
            FROM charm_control.calibration_reports ORDER BY created_at DESC LIMIT 1
            """
        )
        calibration = cur.fetchone()
        cur.execute(
            """
            SELECT report_id,paired_candidates,spearman_correlation,kendall_correlation,
                   promotion_false_negative,early_stopping_enabled,created_at
            FROM charm_control.fidelity_reports ORDER BY created_at DESC LIMIT 1
            """
        )
        fidelity = cur.fetchone()
    decision = decide_optimizer_gate(
        bool(calibration and calibration["probabilistic_promotion_enabled"]),
        int(calibration["infeasible_labels"]) if calibration else 0,
        bool(fidelity and fidelity["early_stopping_enabled"]),
    )
    return {
        "decision": decision.decision,
        "reasons": list(decision.reasons),
        "calibration_report": dict(calibration) if calibration else None,
        "fidelity_report": dict(fidelity) if fidelity else None,
    }
