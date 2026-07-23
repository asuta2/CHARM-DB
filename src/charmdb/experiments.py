from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.db import connect

DEFAULT_SEEDS = (20260731, 20260801, 20260802)
REQUIRED_METRICS = (
    "throughput_tps",
    "p99_ms",
    "operational_cost",
    "full_evaluations",
    "unsafe_trials",
    "drift_recovery",
    "calibration",
    "uncertainty",
    "effect_size",
)
ABLATIONS = (
    "no_workload_context",
    "no_drift_detection",
    "no_historical_transfer",
    "no_calibration",
    "no_probabilistic_constraints",
    "no_multi_fidelity_evaluation",
    "no_early_stopping",
    "no_cost_aware_acquisition",
    "no_index_tuning",
    "no_coordinated_selection",
    "no_robust_objective",
    "no_operational_cost_penalty",
)


@dataclass(frozen=True)
class ExperimentGroupSpec:
    key: str
    kind: str
    hypothesis: str
    baseline: str
    arms: tuple[str, ...]
    budget_unit: str
    budget_value: float
    prerequisite_gate: str


def experiment_group_specs() -> tuple[ExperimentGroupSpec, ...]:
    groups = (
        ExperimentGroupSpec(
            "search_methods",
            "COMPARISON",
            "search method changes robust feasible outcomes at equal realized budget",
            "postgresql_default",
            ("postgresql_default", "random", "sobol", "standard_bo", "constrained_bo"),
            "F3-equivalent units per seed",
            20,
            "measurement_validity",
        ),
        ExperimentGroupSpec(
            "multi_fidelity",
            "COMPARISON",
            "adaptive fidelity reduces F3 use without unacceptable robust performance loss",
            "f3_only_bo",
            ("f3_only_bo", "fixed_f2_then_f3", "adaptive_mfbo", "adaptive_mfbo_early_stop"),
            "F3-equivalent units per seed",
            20,
            "paired_fidelity_gate",
        ),
        ExperimentGroupSpec(
            "uncertainty_calibration",
            "COMPARISON",
            "calibrated intervals improve sequential coverage and false-safe control",
            "gp_uncertainty",
            ("gp_uncertainty", "random_forest", "gradient_boosted", "conformalized"),
            "F3-equivalent units per seed",
            20,
            "calibration_gate",
        ),
        ExperimentGroupSpec(
            "action_space",
            "COMPARISON",
            "coordinated knob/index selection improves robust feasible outcomes at equal cost",
            "knob_only",
            (
                "knob_only",
                "index_only",
                "knobs_then_indexes",
                "indexes_then_knobs",
                "alternating",
                "coordinated",
            ),
            "F3-equivalent units per seed",
            20,
            "coordination_execution_gate",
        ),
        ExperimentGroupSpec(
            "drift_adaptation",
            "COMPARISON",
            "context-aware adaptation improves recovery after preregistered workload drift",
            "no_adaptation",
            (
                "no_adaptation",
                "full_retuning",
                "warm_start",
                "historical_champion",
                "proposed_adaptive",
            ),
            "pre-drift plus recovery units per seed",
            60,
            "drift_execution_gate",
        ),
        ExperimentGroupSpec(
            "risk_awareness",
            "COMPARISON",
            "calibrated probabilistic constraints reduce unsafe trials without excessive rejection",
            "hard_constraints",
            (
                "performance_only_with_hard_safety",
                "hard_constraints",
                "probabilistic_constraints",
                "calibrated_probabilistic_constraints",
            ),
            "F3-equivalent units per seed",
            20,
            "calibration_gate",
        ),
        ExperimentGroupSpec(
            "operational_cost",
            "COMPARISON",
            "measured cost awareness reduces realized cost without unacceptable performance loss",
            "no_cost",
            ("no_cost", "benchmark_only", "restart_index_only", "complete_cost"),
            "F3-equivalent units per seed",
            20,
            "cost_measurement_gate",
        ),
    )
    ablations = tuple(
        ExperimentGroupSpec(
            f"ablation_{name}",
            "ABLATION",
            "removing "
            f"{name.removeprefix('no_').replace('_', ' ')} changes full-pipeline outcomes "
            "at equal realized budget",
            "full_pipeline",
            ("full_pipeline", name),
            "F3-equivalent units per seed",
            20,
            "full_pipeline_gate",
        )
        for name in ABLATIONS
    )
    return groups + ablations


def deterministic_arm_order(arms: tuple[str, ...], seed: int) -> tuple[str, ...]:
    ordered = list(arms)
    random.Random(seed).shuffle(ordered)
    return tuple(ordered)


def register_experiment_matrix(
    settings: Settings,
    seeds: tuple[int, ...] = DEFAULT_SEEDS,
) -> dict[str, int]:
    if len(seeds) < 3 or len(set(seeds)) != len(seeds):
        raise ValueError("experiment registry requires at least three unique seeds")
    specs = experiment_group_specs()
    arm_count = 0
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        for spec in specs:
            group_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:experiment:{spec.key}")
            cur.execute(
                """
                INSERT INTO charm_control.experiment_groups
                (group_id,group_key,group_kind,hypothesis,baseline,budget_unit,budget_value,seeds,
                 required_metrics,prerequisite_gate,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PLANNED')
                ON CONFLICT (group_key) DO UPDATE SET
                    hypothesis=EXCLUDED.hypothesis,baseline=EXCLUDED.baseline,
                    budget_unit=EXCLUDED.budget_unit,budget_value=EXCLUDED.budget_value,
                    seeds=EXCLUDED.seeds,required_metrics=EXCLUDED.required_metrics,
                    prerequisite_gate=EXCLUDED.prerequisite_gate
                WHERE charm_control.experiment_groups.status='PLANNED'
                """,
                (
                    group_id,
                    spec.key,
                    spec.kind,
                    spec.hypothesis,
                    spec.baseline,
                    spec.budget_unit,
                    spec.budget_value,
                    Jsonb(seeds),
                    Jsonb(REQUIRED_METRICS),
                    spec.prerequisite_gate,
                ),
            )
            for seed in seeds:
                for block_order, label in enumerate(deterministic_arm_order(spec.arms, seed)):
                    arm_id = uuid.uuid5(
                        uuid.NAMESPACE_URL, f"charmdb:experiment:{spec.key}:{seed}:{label}"
                    )
                    cur.execute(
                        """
                        INSERT INTO charm_control.experiment_arms
                        (arm_id,group_id,label,method_definition,random_seed,block_order,
                         budget_value,status)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,'PLANNED')
                        ON CONFLICT (group_id,label,random_seed) DO UPDATE SET
                            method_definition=EXCLUDED.method_definition,
                            block_order=EXCLUDED.block_order,budget_value=EXCLUDED.budget_value
                        WHERE charm_control.experiment_arms.status='PLANNED'
                        """,
                        (
                            arm_id,
                            group_id,
                            label,
                            Jsonb({"label": label, "hard_safety_always_active": True}),
                            seed,
                            block_order,
                            spec.budget_value,
                        ),
                    )
                    arm_count += 1
        conn.commit()
    return {"groups": len(specs), "comparison_groups": 7, "ablations": 12, "arms": arm_count}


def experiment_registry_status(settings: Settings) -> list[dict[str, Any]]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT g.group_id,g.group_key,g.group_kind,g.hypothesis,g.baseline,g.budget_unit,
                   g.budget_value,g.seeds,g.required_metrics,g.prerequisite_gate,g.status,
                   g.blocked_reason,count(a.arm_id) AS arm_count,
                   count(a.arm_id) FILTER (WHERE a.status='COMPLETED') AS completed_arms
            FROM charm_control.experiment_groups g
            LEFT JOIN charm_control.experiment_arms a USING(group_id)
            GROUP BY g.group_id ORDER BY g.group_kind,g.group_key
            """
        )
        return [dict(row) for row in cur.fetchall()]
