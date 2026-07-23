from __future__ import annotations

import hashlib
import json
import math
import random
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Literal

from psycopg.types.json import Jsonb
from torch import double, tensor

from charmdb.arm_restore import ensure_arm_dataset_restored
from charmdb.config import Settings
from charmdb.db import connect
from charmdb.experiment_execution import (
    MEASURED_F3_TERMINAL_STATES,
    finalize_experiment_arm,
    record_experiment_trial_budget,
)
from charmdb.multiobjective import recommend_multiobjective
from charmdb.optimizer import Candidate, Observation, decode_vector, sobol_candidates
from charmdb.worker import (
    TERMINAL_STATES,
    control_campaign,
    create_baseline_benchmark_trial,
    create_tuned_benchmark_trial,
    run_once,
)

SearchMethod = Literal["postgresql_default", "random", "sobol", "standard_bo", "constrained_bo"]
SEARCH_METHODS = frozenset(
    {"postgresql_default", "random", "sobol", "standard_bo", "constrained_bo"}
)
SEARCH_STAGES = frozenset(
    {"DEFAULT", "RANDOM", "SOBOL", "BO_INITIALIZATION", "STANDARD_BO", "CONSTRAINED_BO"}
)


@dataclass(frozen=True)
class SearchRecommendation:
    method: SearchMethod
    stage: str
    budget_position: int
    random_seed: int
    candidate: Candidate | None
    training_observation_ids: tuple[uuid.UUID, ...] = ()
    acquisition_name: str | None = None
    acquisition_value: float | None = None
    probability_feasible: float | None = None


@dataclass(frozen=True)
class SearchArmProgress:
    arm_id: uuid.UUID
    campaign_id: uuid.UUID
    method: SearchMethod
    status: str
    realized_budget_value: float
    scheduled_positions: int
    trial_id: uuid.UUID | None
    action: str


def search_trial_idempotency_key(
    arm_id: uuid.UUID, seed: int, method: SearchMethod, budget_position: int
) -> str:
    if budget_position < 0:
        raise ValueError("budget position must be non-negative")
    return f"experiment:{arm_id}:{seed}:{method}:{budget_position}"


def _random_candidates(count: int, seed: int) -> list[Candidate]:
    generator = random.Random(seed)
    candidates: list[Candidate] = []
    seen: set[str] = set()
    while len(candidates) < count:
        candidate = decode_vector(
            tensor([generator.random(), generator.random(), generator.random()])
        )
        key = json.dumps(candidate.configuration, sort_keys=True)
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)
    return candidates


def _sobol_candidate_at_position(position: int, seed: int) -> Candidate:
    draw_count = position + 1
    while draw_count <= 1_048_576:
        candidates = sobol_candidates(draw_count, seed)
        if len(candidates) > position:
            return candidates[position]
        draw_count *= 2
    raise RuntimeError("Sobol schedule could not produce enough unique decoded configurations")


def recommend_search_position(
    method: SearchMethod,
    seed: int,
    budget_position: int,
    observations: list[Observation],
    *,
    excluded_configurations: set[str] | None = None,
    initial_observations: int = 4,
    pool_size: int = 2048,
    sample_count: int = 256,
    reference_point: tuple[float, float] = (0.0, -20.0),
    p99_slo_ms: float = 20.0,
) -> SearchRecommendation:
    if method not in SEARCH_METHODS:
        raise ValueError(f"unsupported search method {method}")
    if budget_position < 0:
        raise ValueError("budget position must be non-negative")
    if initial_observations < 4:
        raise ValueError("BO initialization requires at least four measured observations")
    if method == "postgresql_default":
        return SearchRecommendation(method, "DEFAULT", budget_position, seed, None)
    if method == "random":
        candidate = _random_candidates(budget_position + 1, seed)[budget_position]
        return SearchRecommendation(method, "RANDOM", budget_position, seed, candidate)
    if method == "sobol":
        candidate = _sobol_candidate_at_position(budget_position, seed)
        return SearchRecommendation(method, "SOBOL", budget_position, seed, candidate)
    if len(observations) < initial_observations:
        candidate = _sobol_candidate_at_position(budget_position, seed)
        return SearchRecommendation(method, "BO_INITIALIZATION", budget_position, seed, candidate)
    excluded = {
        json.dumps(observation.configuration, sort_keys=True) for observation in observations
    }
    excluded.update(excluded_configurations or set())
    training_ids = tuple(observation.observation_id for observation in observations)
    recommendation_seed = seed + budget_position
    constrained = method == "constrained_bo"
    candidate, value, probability = recommend_multiobjective(
        observations,
        recommendation_seed,
        excluded,
        "qLogNEHVI",
        pool_size,
        sample_count,
        reference_point,
        constrained=constrained,
        p99_slo_ms=p99_slo_ms,
    )
    return SearchRecommendation(
        method,
        "CONSTRAINED_BO" if constrained else "STANDARD_BO",
        budget_position,
        recommendation_seed,
        candidate,
        training_ids,
        "qLogNEHVI_CONSTRAINED" if constrained else "qLogNEHVI",
        value,
        probability,
    )


def _load_arm(settings: Settings, arm_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.*,g.group_key,g.status AS group_status
            FROM charm_control.experiment_arms a
            JOIN charm_control.experiment_groups g USING(group_id)
            WHERE a.arm_id=%s
            """,
            (arm_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown experiment arm {arm_id}")
    return dict(row)


def _validate_search_arm(arm: dict[str, Any]) -> tuple[SearchMethod, dict[str, Any]]:
    if arm["group_key"] != "search_methods":
        raise ValueError("search executor only accepts arms in the search_methods group")
    method = str(arm["label"])
    if method not in SEARCH_METHODS:
        raise ValueError(f"unsupported search method {method}")
    if arm["status"] not in {"PREPARED", "RUNNING"}:
        raise ValueError(f"search arm must be PREPARED or RUNNING, found {arm['status']}")
    if arm["campaign_id"] is None or arm["execution_manifest"] is None:
        raise ValueError("search arm must have a frozen manifest and linked campaign")
    budget = float(arm["budget_value"])
    if not budget.is_integer():
        raise ValueError("search arm budget must be a whole number of F3 positions")
    manifest = dict(arm["execution_manifest"])
    execution = dict(manifest["execution"])
    accounting = dict(execution["budget_accounting"])
    if accounting["kind"] != "F3_EQUIVALENT_WALL_CLOCK":
        raise ValueError("search executor requires F3-equivalent wall-clock accounting")
    objective = dict(execution["objective_definition"])
    if objective.get("maximize") != ["throughput_tps", "negative_p99_ms"]:
        raise ValueError("search executor requires throughput and negative-p99 objectives")
    if objective.get("reference_point") != [0.0, -20.0]:
        raise ValueError("search executor requires the frozen [0, -20] reference point")
    constraints = dict(execution["constraint_definition"])
    if constraints != {
        "p99_ms_max": 20.0,
        "failures_max": 0,
        "hard_safety_always_active": True,
    }:
        raise ValueError("search executor requires the frozen p99/failure safety constraints")
    return method, execution  # type: ignore[return-value]


def _assert_block_order(settings: Settings, arm: dict[str, Any]) -> None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT label,status,block_order
            FROM charm_control.experiment_arms
            WHERE group_id=%s AND random_seed=%s AND block_order<%s
              AND status<>'COMPLETED'
            ORDER BY block_order
            """,
            (arm["group_id"], arm["random_seed"], arm["block_order"]),
        )
        blockers = [dict(row) for row in cur.fetchall()]
    if blockers:
        detail = ", ".join(
            f"{row['block_order']}:{row['label']}={row['status']}" for row in blockers
        )
        raise ValueError(f"within-seed block order is not satisfied: {detail}")


def _reconcile_terminal_trials(settings: Settings, arm: dict[str, Any], actor: str) -> None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.trial_id
            FROM charm_control.trials t
            WHERE t.campaign_id=%s AND t.completed_at IS NOT NULL
              AND t.state=ANY(%s)
            ORDER BY t.created_at,t.trial_id
            """,
            (arm["campaign_id"], list(TERMINAL_STATES)),
        )
        trial_ids = [row["trial_id"] for row in cur.fetchall()]
    for trial_id in trial_ids:
        reconcile_terminal_search_trial(settings, trial_id, actor)


def _load_existing_progress(
    settings: Settings, arm_id: uuid.UUID
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.*,t.state AS trial_state,t.completed_at
            FROM charm_control.experiment_search_recommendations r
            LEFT JOIN charm_control.trials t USING(trial_id)
            WHERE r.arm_id=%s ORDER BY r.budget_position
            """,
            (arm_id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    active_rows = [row for row in rows if row["trial_id"] is None or row["completed_at"] is None]
    if len(active_rows) > 1:
        raise RuntimeError("search arm contains more than one active recommendation")
    active = active_rows[0] if active_rows else None
    return rows, active


def _recommendation_from_row(row: dict[str, Any]) -> SearchRecommendation:
    method = str(row["method"])
    if method not in SEARCH_METHODS:
        raise ValueError(f"persisted search recommendation has unsupported method {method}")
    stage = str(row["stage"])
    if stage not in SEARCH_STAGES:
        raise ValueError(f"persisted search recommendation has unsupported stage {stage}")
    allowed_stages = {
        "postgresql_default": {"DEFAULT"},
        "random": {"RANDOM"},
        "sobol": {"SOBOL"},
        "standard_bo": {"BO_INITIALIZATION", "STANDARD_BO"},
        "constrained_bo": {"BO_INITIALIZATION", "CONSTRAINED_BO"},
    }
    if stage not in allowed_stages[method]:
        raise ValueError("persisted recommendation stage does not match its method")
    raw_vector = row["input_vector"]
    configuration = {str(key): str(value) for key, value in dict(row["configuration"]).items()}
    candidate: Candidate | None
    if raw_vector is None:
        if configuration:
            raise ValueError("baseline recommendation has a configuration without an input vector")
        candidate = None
    else:
        vector = tuple(float(value) for value in raw_vector)
        if len(vector) != 3 or any(value < 0 or value > 1 for value in vector):
            raise ValueError(
                "persisted recommendation input vector is outside the executable space"
            )
        decoded = decode_vector(tensor(vector, dtype=double))
        if decoded.configuration != configuration:
            raise ValueError("persisted recommendation vector and configuration do not match")
        candidate = Candidate((vector[0], vector[1], vector[2]), configuration)
    if (method == "postgresql_default") != (candidate is None):
        raise ValueError("persisted recommendation candidate does not match its method")
    training_ids = tuple(uuid.UUID(str(value)) for value in row["training_observation_ids"])
    acquisition_value = (
        float(row["acquisition_value"]) if row["acquisition_value"] is not None else None
    )
    probability_feasible = (
        float(row["probability_feasible"]) if row["probability_feasible"] is not None else None
    )
    if acquisition_value is not None and not math.isfinite(acquisition_value):
        raise ValueError("persisted recommendation acquisition value is not finite")
    if probability_feasible is not None and not 0 <= probability_feasible <= 1:
        raise ValueError("persisted recommendation feasibility probability is outside [0, 1]")
    return SearchRecommendation(
        method,  # type: ignore[arg-type]
        stage,
        int(row["budget_position"]),
        int(row["random_seed"]),
        candidate,
        training_ids,
        str(row["acquisition_name"]) if row["acquisition_name"] is not None else None,
        acquisition_value,
        probability_feasible,
    )


def _persist_recommendation(
    settings: Settings, arm_id: uuid.UUID, recommendation: SearchRecommendation
) -> uuid.UUID:
    recommendation_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"charmdb:search-recommendation:{arm_id}:{recommendation.budget_position}",
    )
    candidate = recommendation.candidate
    for name, value in (
        ("acquisition_value", recommendation.acquisition_value),
        ("probability_feasible", recommendation.probability_feasible),
    ):
        if value is not None and not math.isfinite(value):
            raise ValueError(f"search recommendation {name} must be finite")
    if (
        recommendation.probability_feasible is not None
        and not 0 <= recommendation.probability_feasible <= 1
    ):
        raise ValueError("search recommendation probability_feasible must be within [0, 1]")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO charm_control.experiment_search_recommendations
            (recommendation_id,arm_id,budget_position,method,stage,random_seed,input_vector,
             configuration,training_observation_ids,acquisition_name,acquisition_value,
             probability_feasible)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (arm_id,budget_position) DO NOTHING
            RETURNING recommendation_id
            """,
            (
                recommendation_id,
                arm_id,
                recommendation.budget_position,
                recommendation.method,
                recommendation.stage,
                recommendation.random_seed,
                Jsonb(list(candidate.vector)) if candidate else None,
                Jsonb(candidate.configuration if candidate else {}),
                Jsonb([str(item) for item in recommendation.training_observation_ids]),
                recommendation.acquisition_name,
                recommendation.acquisition_value,
                recommendation.probability_feasible,
            ),
        )
        inserted = cur.fetchone()
        if inserted is None:
            cur.execute(
                """
                SELECT recommendation_id,budget_position,method,stage,random_seed,input_vector,
                       configuration,training_observation_ids,acquisition_name,
                       acquisition_value,probability_feasible
                FROM charm_control.experiment_search_recommendations
                WHERE arm_id=%s AND budget_position=%s
                """,
                (arm_id, recommendation.budget_position),
            )
            existing = cur.fetchone()
            expected = {
                "method": recommendation.method,
                "stage": recommendation.stage,
                "random_seed": recommendation.random_seed,
                "input_vector": list(candidate.vector) if candidate else None,
                "configuration": candidate.configuration if candidate else {},
                "training_observation_ids": [
                    str(item) for item in recommendation.training_observation_ids
                ],
                "acquisition_name": recommendation.acquisition_name,
            }
            if existing is None or any(existing[key] != value for key, value in expected.items()):
                raise ValueError("persisted search recommendation conflicts with reconstruction")
            for key, value in (
                ("acquisition_value", recommendation.acquisition_value),
                ("probability_feasible", recommendation.probability_feasible),
            ):
                persisted = existing[key]
                if (persisted is None) != (value is None) or (
                    persisted is not None
                    and value is not None
                    and not math.isclose(float(persisted), value, rel_tol=1e-12, abs_tol=1e-12)
                ):
                    raise ValueError(
                        "persisted search recommendation conflicts with acquisition lineage"
                    )
            recommendation_id = existing["recommendation_id"]
        conn.commit()
    return uuid.UUID(str(recommendation_id))


def _create_or_link_trial(
    settings: Settings,
    arm: dict[str, Any],
    execution: dict[str, Any],
    recommendation: SearchRecommendation,
) -> uuid.UUID:
    profile = dict(execution["execution_profile"])
    constraints = dict(execution["constraint_definition"])
    method_parameters = dict(execution["method_parameters"])
    key = search_trial_idempotency_key(
        arm["arm_id"],
        int(arm["random_seed"]),
        recommendation.method,
        recommendation.budget_position,
    )
    trial_seed = int(arm["random_seed"]) + recommendation.budget_position
    common = {
        "settings": settings,
        "campaign_id": arm["campaign_id"],
        "seed": trial_seed,
        "idempotency_key": key,
        "warmup_seconds": int(profile["warmup_seconds"]),
        "duration_seconds": int(profile["measurement_seconds"]),
        "concurrency": int(profile["concurrency"]),
        "p99_slo_ms": float(constraints["p99_ms_max"]),
        "max_attempts": int(method_parameters["max_attempts"]),
    }
    if recommendation.candidate is None:
        trial_id = create_baseline_benchmark_trial(fidelity=3, **common)
    else:
        trial_id = create_tuned_benchmark_trial(
            candidate=recommendation.candidate.configuration, **common
        )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE charm_control.experiment_search_recommendations
            SET trial_id=%s WHERE arm_id=%s AND budget_position=%s
              AND (trial_id IS NULL OR trial_id=%s)
            """,
            (trial_id, arm["arm_id"], recommendation.budget_position, trial_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("search recommendation is linked to a different trial")
        cur.execute(
            """
            UPDATE charm_control.trials
            SET diagnostic_details=diagnostic_details || %s
            WHERE trial_id=%s
            """,
            (
                Jsonb(
                    {
                        "experiment_arm_id": str(arm["arm_id"]),
                        "budget_position": recommendation.budget_position,
                        "search_method": recommendation.method,
                        "search_stage": recommendation.stage,
                    }
                ),
                trial_id,
            ),
        )
        conn.commit()
    return trial_id


def _observations_for_arm(
    settings: Settings,
    arm_id: uuid.UUID,
    campaign_id: uuid.UUID,
    p99_slo_ms: float,
) -> list[Observation]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.budget_position,r.input_vector AS recommendation_vector,
                   r.configuration AS recommendation_configuration,t.trial_id,t.state,
                   t.fidelity,t.requested_configuration,t.active_configuration,
                   o.observation_id,o.input_vector,o.configuration,o.throughput_tps,o.p99_ms,
                   o.failures,o.feasible,o.status
            FROM charm_control.experiment_search_recommendations r
            JOIN charm_control.trials t USING(trial_id)
            JOIN charm_control.optimization_observations o
              ON o.benchmark_trial_id=t.trial_id AND o.campaign_id=t.campaign_id
            WHERE r.arm_id=%s AND t.campaign_id=%s AND t.completed_at IS NOT NULL
              AND t.fidelity=3 AND t.state=ANY(%s)
              AND o.throughput_tps IS NOT NULL AND o.p99_ms IS NOT NULL
              AND o.status IN ('COMPLETED','INFEASIBLE')
            ORDER BY r.budget_position,o.observation_id
            """,
            (arm_id, campaign_id, list(MEASURED_F3_TERMINAL_STATES)),
        )
        rows = [dict(row) for row in cur.fetchall()]
    positions: set[int] = set()
    artifact_root = settings.artifact_dir.resolve()
    observations: list[Observation] = []
    for row in rows:
        position = int(row["budget_position"])
        if position in positions:
            raise RuntimeError(f"search position {position} has multiple measured observations")
        positions.add(position)
        vector = tuple(float(value) for value in row["input_vector"])
        recommendation_vector = tuple(float(value) for value in row["recommendation_vector"])
        configuration = {str(key): str(value) for key, value in dict(row["configuration"]).items()}
        recommendation_configuration = {
            str(key): str(value) for key, value in dict(row["recommendation_configuration"]).items()
        }
        if vector != recommendation_vector or configuration != recommendation_configuration:
            raise RuntimeError(f"search position {position} observation lineage does not match")
        requested = {str(key): str(value) for key, value in row["requested_configuration"].items()}
        active = {str(key): str(value) for key, value in row["active_configuration"].items()}
        if requested != recommendation_configuration or any(
            active.get(name) != value for name, value in requested.items()
        ):
            raise RuntimeError(f"search position {position} active configuration does not match")
        failures = int(row["failures"])
        p99_ms = float(row["p99_ms"])
        expected_feasible = failures == 0 and p99_ms <= p99_slo_ms
        if bool(row["feasible"]) != expected_feasible:
            raise RuntimeError(f"search position {position} has inconsistent feasibility evidence")
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT relative_path,sha256,byte_size FROM charm_control.artifacts
                   WHERE trial_id=%s ORDER BY artifact_id""",
                (row["trial_id"],),
            )
            artifacts = [dict(artifact) for artifact in cur.fetchall()]
        if not artifacts:
            raise RuntimeError(f"search position {position} has no registered raw artifact")
        for artifact in artifacts:
            path = (settings.artifact_dir / str(artifact["relative_path"])).resolve()
            try:
                path.relative_to(artifact_root)
            except ValueError as exc:
                raise RuntimeError(
                    f"search position {position} artifact escapes the artifact root"
                ) from exc
            if (
                not path.is_file()
                or path.stat().st_size != int(artifact["byte_size"])
                or hashlib.sha256(path.read_bytes()).hexdigest() != str(artifact["sha256"])
            ):
                raise RuntimeError(f"search position {position} artifact hash evidence is invalid")
        observations.append(
            Observation(
                uuid.UUID(str(row["observation_id"])),
                (vector[0], vector[1], vector[2]),
                configuration,
                float(row["throughput_tps"]),
                p99_ms,
                failures,
                expected_feasible,
            )
        )
    return observations


def _persist_terminal_observation(settings: Settings, trial_id: uuid.UUID) -> None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.arm_id,r.budget_position,r.method,r.input_vector,r.configuration,
                   r.acquisition_name,r.acquisition_value,r.probability_feasible,
                   r.training_observation_ids,a.campaign_id,t.random_seed,t.fidelity,t.state,
                   t.objective_values,t.constraint_values,t.failure_type,t.diagnostic_details
            FROM charm_control.experiment_search_recommendations r
            JOIN charm_control.experiment_arms a USING(arm_id)
            JOIN charm_control.trials t USING(trial_id)
            WHERE r.trial_id=%s
            """,
            (trial_id,),
        )
        row = cur.fetchone()
        if row is None:
            return
        objectives = dict(row["objective_values"] or {})
        constraints = dict(row["constraint_values"] or {})
        throughput = objectives.get("throughput_tps")
        p99 = objectives.get("p99_ms")
        failures = constraints.get("failures")
        feasible = bool(constraints.get("feasible", False))
        if throughput is not None and p99 is not None and failures is not None:
            status = "COMPLETED" if feasible else "INFEASIBLE"
        else:
            status = "FAILED"
            feasible = False
        observation_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"charmdb:search-observation:{row['arm_id']}:{trial_id}"
        )
        cur.execute(
            """
            INSERT INTO charm_control.optimization_observations
            (observation_id,campaign_id,benchmark_trial_id,method,iteration,random_seed,
             input_vector,configuration,throughput_tps,p99_ms,p99_margin_ms,failures,feasible,
             acquisition_name,acquisition_value,probability_feasible,training_observation_ids,
             status,diagnostic_details,workload_context,fidelity)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (observation_id) DO NOTHING
            """,
            (
                observation_id,
                row["campaign_id"],
                trial_id,
                str(row["method"]).upper(),
                row["budget_position"],
                row["random_seed"],
                Jsonb(row["input_vector"] or []),
                Jsonb(row["configuration"]),
                throughput,
                p99,
                constraints.get("p99_margin_ms"),
                failures,
                feasible,
                row["acquisition_name"],
                row["acquisition_value"],
                row["probability_feasible"],
                Jsonb(row["training_observation_ids"]),
                status,
                Jsonb(
                    {
                        "experiment_arm_id": str(row["arm_id"]),
                        "terminal_trial_state": row["state"],
                        "failure_type": row["failure_type"],
                        "trial_diagnostic_details": row["diagnostic_details"],
                    }
                ),
                Jsonb({}),
                row["fidelity"],
            ),
        )
        conn.commit()


def reconcile_terminal_search_trial(
    settings: Settings, trial_id: uuid.UUID, actor: str = "worker"
) -> dict[str, Any] | None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.arm_id,t.completed_at,t.state
            FROM charm_control.experiment_search_recommendations r
            JOIN charm_control.trials t USING(trial_id)
            WHERE r.trial_id=%s
            """,
            (trial_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if row["completed_at"] is None or str(row["state"]) not in TERMINAL_STATES:
        raise ValueError("search trial is not terminal and cannot be reconciled")
    _persist_terminal_observation(settings, trial_id)
    return record_experiment_trial_budget(settings, row["arm_id"], trial_id, actor=actor)


def start_or_resume_search_arm(
    settings: Settings, arm_id: uuid.UUID, actor: str = "cli"
) -> SearchArmProgress:
    if not actor.strip():
        raise ValueError("actor cannot be empty")
    arm = _load_arm(settings, arm_id)
    method, execution = _validate_search_arm(arm)
    _assert_block_order(settings, arm)
    lineage = execution.get("evidence_lineage")
    if lineage is not None:
        if not isinstance(lineage, dict) or not isinstance(lineage.get("preflight_id"), str):
            raise ValueError("search manifest has malformed evidence lineage")
        ensure_arm_dataset_restored(
            settings,
            arm_id,
            uuid.UUID(lineage["preflight_id"]),
            str(arm["manifest_sha256"]),
        )
    campaign_id = uuid.UUID(str(arm["campaign_id"]))
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM charm_control.campaigns WHERE campaign_id=%s", (campaign_id,)
        )
        campaign = cur.fetchone()
    if campaign is None:
        raise ValueError("linked experiment campaign is missing")
    _reconcile_terminal_trials(settings, arm, actor)
    arm = _load_arm(settings, arm_id)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM charm_control.campaigns WHERE campaign_id=%s", (campaign_id,)
        )
        campaign = cur.fetchone()
    if campaign is None:
        raise ValueError("linked experiment campaign is missing after reconciliation")
    if campaign["status"] in {"CREATED", "PAUSED"}:
        control_campaign(settings, campaign_id, "resume", "search arm start/resume", actor)
    elif campaign["status"] != "RUNNING":
        raise ValueError(f"search campaign cannot resume from {campaign['status']}")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_groups SET status='RUNNING'
               WHERE group_id=%s AND status IN ('ELIGIBLE','RUNNING')""",
            (arm["group_id"],),
        )
        conn.commit()
    rows, active = _load_existing_progress(settings, arm_id)
    if active is not None:
        trial = active.get("trial_id")
        action = "RESUMED_ACTIVE_TRIAL"
        if trial is None:
            persisted = _recommendation_from_row(active)
            if persisted.method != method:
                raise RuntimeError("active recommendation method does not match its experiment arm")
            trial = _create_or_link_trial(settings, arm, execution, persisted)
            action = "RECOVERED_UNLINKED_RECOMMENDATION"
        return SearchArmProgress(
            arm_id,
            campaign_id,
            method,
            str(arm["status"]),
            float(arm["realized_budget_value"]),
            len(rows),
            uuid.UUID(str(trial)),
            action,
        )
    position = len(rows)
    target_budget = float(arm["budget_value"])
    if float(arm["realized_budget_value"]) + 1e-9 >= target_budget:
        finalized = finalize_experiment_arm(settings, arm_id, actor)
        return SearchArmProgress(
            arm_id,
            campaign_id,
            method,
            str(finalized["status"]),
            float(finalized["realized_budget_value"]),
            position,
            None,
            "FINALIZED",
        )
    parameters = dict(execution["method_parameters"])
    objective = dict(execution["objective_definition"])
    constraints = dict(execution["constraint_definition"])
    reference = tuple(float(value) for value in objective["reference_point"])
    observations = (
        _observations_for_arm(
            settings,
            arm_id,
            campaign_id,
            float(constraints["p99_ms_max"]),
        )
        if method in {"standard_bo", "constrained_bo"}
        else []
    )
    excluded_configurations = {
        json.dumps(dict(row["configuration"]), sort_keys=True) for row in rows
    }
    recommendation = recommend_search_position(
        method,
        int(arm["random_seed"]),
        position,
        observations,
        excluded_configurations=excluded_configurations,
        initial_observations=int(parameters["initial_observations"]),
        pool_size=int(parameters["candidate_pool_size"]),
        sample_count=int(parameters["posterior_samples"]),
        reference_point=(reference[0], reference[1]),
        p99_slo_ms=float(constraints["p99_ms_max"]),
    )
    _persist_recommendation(settings, arm_id, recommendation)
    trial_id = _create_or_link_trial(settings, arm, execution, recommendation)
    return SearchArmProgress(
        arm_id,
        campaign_id,
        method,
        str(arm["status"]),
        float(arm["realized_budget_value"]),
        position + 1,
        trial_id,
        "SCHEDULED",
    )


def run_search_arm(
    settings: Settings,
    arm_id: uuid.UUID,
    actor: str = "search-orchestrator",
    owner: str = "search-arm-worker",
) -> SearchArmProgress:
    while True:
        progress = start_or_resume_search_arm(settings, arm_id, actor)
        if progress.action == "FINALIZED":
            return progress
        result = run_once(
            settings,
            owner=owner,
            lease_seconds=120,
            campaign_id=progress.campaign_id,
        )
        if result is None or not result.claimed:
            time.sleep(1.0)


def progress_json(progress: SearchArmProgress) -> dict[str, Any]:
    payload = asdict(progress)
    return {
        key: str(value) if isinstance(value, uuid.UUID) else value for key, value in payload.items()
    }
