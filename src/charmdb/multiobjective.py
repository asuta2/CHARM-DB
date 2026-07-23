from __future__ import annotations

import json
import math
import statistics
import uuid
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any, Literal

import torch
from botorch import fit_gpytorch_mll
from botorch.acquisition import AcquisitionFunction
from botorch.acquisition.multi_objective.logei import qLogNoisyExpectedHypervolumeImprovement
from botorch.acquisition.multi_objective.objective import IdentityMCMultiOutputObjective
from botorch.acquisition.multi_objective.parego import qLogNParEGO
from botorch.models import ModelListGP, SingleTaskGP
from botorch.models.transforms.outcome import Standardize
from botorch.sampling.normal import SobolQMCNormalSampler
from botorch.utils.multi_objective.hypervolume import Hypervolume
from gpytorch.mlls.sum_marginal_log_likelihood import (  # type: ignore[import-untyped]
    SumMarginalLogLikelihood,
)
from psycopg.types.json import Jsonb
from torch import Tensor

from charmdb.config import Settings
from charmdb.db import connect
from charmdb.gating import persist_optimizer_gate_decision
from charmdb.optimizer import (
    Candidate,
    Observation,
    decode_vector,
    evaluate_candidate,
    sobol_candidates,
)

DTYPE = torch.double
P99_SLO_MS = 20.0
REFERENCE_POINT = (0.0, -20.0)
AcquisitionName = Literal["qLogNEHVI", "qLogNParEGO"]


def _p99_constraint(samples: Tensor) -> Tensor:
    return samples[..., 2]


@dataclass(frozen=True)
class MultiObjectiveResult:
    run_id: uuid.UUID
    campaign_id: uuid.UUID
    observations: tuple[Observation, ...]
    pareto_observation_ids: tuple[uuid.UUID, ...]
    hypervolume: float
    champion_id: uuid.UUID
    champion_status: str


def feasible_pareto(observations: list[Observation]) -> list[Observation]:
    feasible = [item for item in observations if item.feasible and item.failures == 0]
    result: list[Observation] = []
    for candidate in feasible:
        dominated = any(
            other.observation_id != candidate.observation_id
            and other.throughput_tps >= candidate.throughput_tps
            and other.p99_ms <= candidate.p99_ms
            and (other.throughput_tps > candidate.throughput_tps or other.p99_ms < candidate.p99_ms)
            for other in feasible
        )
        if not dominated:
            result.append(candidate)
    return sorted(result, key=lambda item: (item.throughput_tps, -item.p99_ms))


def pareto_hypervolume(
    observations: list[Observation], reference_point: tuple[float, float] = REFERENCE_POINT
) -> float:
    pareto = feasible_pareto(observations)
    points = [
        [item.throughput_tps, -item.p99_ms]
        for item in pareto
        if item.throughput_tps > reference_point[0] and -item.p99_ms > reference_point[1]
    ]
    if not points:
        return 0.0
    return float(
        Hypervolume(torch.tensor(reference_point, dtype=DTYPE)).compute(
            torch.tensor(points, dtype=DTYPE)
        )
    )


def reconstruct_observations(settings: Settings, campaign_id: uuid.UUID) -> list[Observation]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT observation_id,input_vector,configuration,throughput_tps,p99_ms,failures,feasible
            FROM charm_control.optimization_observations
            WHERE campaign_id=%s AND throughput_tps IS NOT NULL AND p99_ms IS NOT NULL
              AND status IN ('COMPLETED','INFEASIBLE')
            ORDER BY created_at,observation_id
            """,
            (campaign_id,),
        )
        rows = cur.fetchall()
    return [
        Observation(
            row["observation_id"],
            tuple(float(value) for value in row["input_vector"]),  # type: ignore[arg-type]
            {str(key): str(value) for key, value in row["configuration"].items()},
            float(row["throughput_tps"]),
            float(row["p99_ms"]),
            int(row["failures"]),
            bool(row["feasible"]),
        )
        for row in rows
    ]


def recommend_multiobjective(
    observations: list[Observation],
    seed: int,
    excluded: set[str],
    acquisition_name: AcquisitionName = "qLogNEHVI",
    pool_size: int = 1024,
    sample_count: int = 128,
    reference_point: tuple[float, float] = REFERENCE_POINT,
    *,
    constrained: bool = True,
    p99_slo_ms: float = P99_SLO_MS,
) -> tuple[Candidate, float, float]:
    if len(observations) < 4:
        raise ValueError("at least four observations are required for multi-objective GP fitting")
    if pool_size < 16 or sample_count < 16:
        raise ValueError("pool_size and sample_count must each be at least 16")
    if not math.isfinite(p99_slo_ms) or p99_slo_ms <= 0:
        raise ValueError("p99_slo_ms must be a positive finite value")
    torch.manual_seed(seed)
    train_x = torch.tensor([item.vector for item in observations], dtype=DTYPE)
    throughput = torch.tensor([[item.throughput_tps] for item in observations], dtype=DTYPE)
    negative_p99 = torch.tensor([[-item.p99_ms] for item in observations], dtype=DTYPE)
    constraint = torch.tensor([[item.p99_ms - p99_slo_ms] for item in observations], dtype=DTYPE)
    objective_gp = SingleTaskGP(train_x, throughput, outcome_transform=Standardize(m=1))
    latency_gp = SingleTaskGP(train_x, negative_p99, outcome_transform=Standardize(m=1))
    constraint_gp = SingleTaskGP(train_x, constraint, outcome_transform=Standardize(m=1))
    model = ModelListGP(objective_gp, latency_gp, constraint_gp)
    fit_gpytorch_mll(SumMarginalLogLikelihood(model.likelihood, model))
    sampler = SobolQMCNormalSampler(sample_shape=torch.Size([sample_count]), seed=seed)
    objective = IdentityMCMultiOutputObjective(outcomes=[0, 1])
    acquisition: AcquisitionFunction
    if acquisition_name == "qLogNEHVI":
        acquisition = qLogNoisyExpectedHypervolumeImprovement(
            model=model,
            ref_point=list(reference_point),
            X_baseline=train_x,
            sampler=sampler,
            objective=objective,
            constraints=[_p99_constraint] if constrained else None,
            prune_baseline=True,
        )
    elif acquisition_name == "qLogNParEGO":
        generator = torch.Generator().manual_seed(seed)
        weights = torch.rand(2, dtype=DTYPE, generator=generator)
        weights /= weights.sum()
        acquisition = qLogNParEGO(
            model=model,
            X_baseline=train_x,
            scalarization_weights=weights,
            sampler=sampler,
            objective=objective,
            constraints=[_p99_constraint] if constrained else None,
            prune_baseline=True,
        )
    else:
        raise ValueError(f"unsupported multi-objective acquisition {acquisition_name}")
    pool = torch.quasirandom.SobolEngine(  # type: ignore[no-untyped-call]
        3, scramble=True, seed=seed + 1000
    ).draw(pool_size, dtype=DTYPE)
    with torch.no_grad():
        values = acquisition(pool.unsqueeze(1)).reshape(-1)
        posterior = constraint_gp.posterior(pool)
        normal = torch.distributions.Normal(0.0, 1.0)
        probability = normal.cdf(  # type: ignore[no-untyped-call]
            -posterior.mean.squeeze(-1) / posterior.variance.squeeze(-1).sqrt().clamp_min(1e-12)
        )
        for index in torch.argsort(values, descending=True).tolist():
            candidate = decode_vector(pool[index])
            if json.dumps(candidate.configuration, sort_keys=True) not in excluded:
                return candidate, float(values[index]), float(probability[index])
    raise RuntimeError("candidate pool contained only duplicates")


def _persist_recommendation(
    settings: Settings,
    run_id: uuid.UUID,
    iteration: int,
    method: str,
    candidate: Candidate,
    context: dict[str, object],
    fidelity: int,
    pool_seed: int,
    acquisition_value: float | None,
    probability_feasible: float | None,
    training_ids: list[uuid.UUID],
) -> uuid.UUID:
    recommendation_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{run_id}:{method}:{iteration}")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO charm_control.multi_objective_recommendations
            (recommendation_id,run_id,iteration,method,input_vector,configuration,
             workload_context,fidelity,pool_seed,acquisition_value,probability_feasible,
             training_observation_ids)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                recommendation_id,
                run_id,
                iteration,
                method,
                Jsonb(candidate.vector),
                Jsonb(candidate.configuration),
                Jsonb(context),
                fidelity,
                pool_seed,
                acquisition_value,
                probability_feasible,
                Jsonb([str(item) for item in training_ids]),
            ),
        )
        conn.commit()
    persist_optimizer_gate_decision(settings, recommendation_id)
    return recommendation_id


def _link_recommendation(
    settings: Settings, recommendation_id: uuid.UUID, observation_id: uuid.UUID
) -> None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.multi_objective_recommendations SET observation_id=%s
               WHERE recommendation_id=%s""",
            (observation_id, recommendation_id),
        )
        conn.commit()


def _persist_failed_observation(
    settings: Settings,
    campaign_id: uuid.UUID,
    method: str,
    iteration: int,
    seed: int,
    candidate: Candidate,
    context: dict[str, object],
    fidelity: int,
    recommendation_id: uuid.UUID,
    error: Exception,
) -> uuid.UUID:
    observation_id = uuid.uuid4()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO charm_control.optimization_observations
            (observation_id,campaign_id,method,iteration,random_seed,input_vector,configuration,
             feasible,status,diagnostic_details,workload_context,fidelity,recommendation_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,false,'FAILED',%s,%s,%s,%s)
            """,
            (
                observation_id,
                campaign_id,
                method,
                iteration,
                seed,
                Jsonb(candidate.vector),
                Jsonb(candidate.configuration),
                Jsonb({"error_type": type(error).__name__, "error": str(error)}),
                Jsonb(context),
                fidelity,
                recommendation_id,
            ),
        )
        cur.execute(
            """UPDATE charm_control.multi_objective_recommendations SET observation_id=%s
               WHERE recommendation_id=%s""",
            (observation_id, recommendation_id),
        )
        conn.commit()
    return observation_id


def _evaluate_recommended_candidate(
    settings: Settings,
    campaign_id: uuid.UUID,
    method: str,
    iteration: int,
    seed: int,
    candidate: Candidate,
    context: dict[str, object],
    fidelity: int,
    recommendation_id: uuid.UUID,
    acquisition_name: str | None = None,
    acquisition_value: float | None = None,
    probability_feasible: float | None = None,
    training_ids: list[uuid.UUID] | None = None,
) -> Observation:
    try:
        return evaluate_candidate(
            settings,
            campaign_id,
            method,
            iteration,
            seed,
            candidate,
            acquisition_name,
            acquisition_value,
            probability_feasible,
            training_ids,
            context,
            fidelity,
            recommendation_id,
        )
    except Exception as error:
        _persist_failed_observation(
            settings,
            campaign_id,
            method,
            iteration,
            seed,
            candidate,
            context,
            fidelity,
            recommendation_id,
            error,
        )
        raise


def _persist_pareto_snapshot(
    settings: Settings,
    run_id: uuid.UUID,
    iteration: int,
    observations: list[Observation],
) -> tuple[list[Observation], float]:
    feasible = [item for item in observations if item.feasible]
    pareto = feasible_pareto(observations)
    hypervolume = pareto_hypervolume(observations)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO charm_control.pareto_snapshots
            (snapshot_id,run_id,iteration,reference_point,feasible_observation_ids,
             pareto_observation_ids,hypervolume)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{run_id}:pareto:{iteration}"),
                run_id,
                iteration,
                Jsonb(REFERENCE_POINT),
                Jsonb([str(item.observation_id) for item in feasible]),
                Jsonb([str(item.observation_id) for item in pareto]),
                hypervolume,
            ),
        )
        conn.commit()
    return pareto, hypervolume


def _persist_champion(
    settings: Settings,
    run_id: uuid.UUID,
    selected: Observation,
    f4: list[Observation],
    context_label: str,
) -> tuple[uuid.UUID, str]:
    throughputs = [item.throughput_tps for item in f4]
    p99_values = [item.p99_ms for item in f4]
    mean_tps = statistics.fmean(throughputs)
    mean_p99 = statistics.fmean(p99_values)
    if len(f4) > 1:
        tps_lcb = mean_tps - 1.96 * statistics.stdev(throughputs) / math.sqrt(len(f4))
        p99_ucb = mean_p99 + 1.96 * statistics.stdev(p99_values) / math.sqrt(len(f4))
    else:
        tps_lcb = mean_tps
        p99_ucb = mean_p99
    all_feasible = (
        all(item.feasible and item.failures == 0 for item in f4) and p99_ucb <= P99_SLO_MS
    )
    status = "CANDIDATE" if all_feasible else "REJECTED"
    champion_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{run_id}:champion")
    details = {
        "required_contexts_for_validation": 2,
        "observed_contexts": 1,
        "interpretation": "one-context development candidate; not a robust thesis champion",
    }
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO charm_control.champion_selections
            (champion_id,run_id,selected_observation_id,rule,f4_observation_ids,context_scores,
             robust_score,worst_p99_ms,status,details)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                champion_id,
                run_id,
                selected.observation_id,
                "feasible Pareto member; max F3 throughput, then min p99; "
                "F4 lower-throughput/upper-p99 bounds",
                Jsonb([str(item.observation_id) for item in f4]),
                Jsonb(
                    {
                        context_label: {
                            "replicates": len(f4),
                            "throughput_mean": mean_tps,
                            "throughput_lcb": tps_lcb,
                            "p99_mean": mean_p99,
                            "p99_ucb": p99_ucb,
                        }
                    }
                ),
                tps_lcb,
                p99_ucb,
                status,
                Jsonb(details),
            ),
        )
        conn.commit()
    return champion_id, status


def run_multiobjective_campaign(
    settings: Settings,
    sobol_trials: int,
    bo_trials: int,
    seed: int,
    context_label: str = "development-pgbench",
    f4_replicates: int = 2,
    acquisition_name: AcquisitionName = "qLogNEHVI",
    pool_size: int = 1024,
    sample_count: int = 128,
) -> MultiObjectiveResult:
    if sobol_trials < 4 or bo_trials < 1 or f4_replicates < 2:
        raise ValueError("multi-objective run requires >=4 Sobol, >=1 BO, and >=2 F4 replicates")
    run_id = uuid.uuid4()
    campaign_id = uuid.uuid4()
    context: dict[str, object] = {"label": context_label, "version": 1}
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO charm_control.campaigns
            (campaign_id,name,mode,status,objective_definition,constraint_definition)
            VALUES (%s,%s,'CONSTRAINED_MULTI','RUNNING',%s,%s)
            """,
            (
                campaign_id,
                f"constrained-multi-{acquisition_name}-seed-{seed}",
                Jsonb({"maximize": ["throughput_tps", "negative_p99_ms"]}),
                Jsonb({"p99_ms_max": P99_SLO_MS, "failures_max": 0}),
            ),
        )
        cur.execute(
            """
            INSERT INTO charm_control.multi_objective_runs
            (run_id,campaign_id,random_seed,reference_point,objective_definition,
             constraint_definition,candidate_pool_size,sample_count,software_versions,status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'RUNNING')
            """,
            (
                run_id,
                campaign_id,
                seed,
                Jsonb(REFERENCE_POINT),
                Jsonb({"maximize": ["throughput_tps", "negative_p99_ms"]}),
                Jsonb({"p99_ms_max": P99_SLO_MS, "failures_max": 0}),
                pool_size,
                sample_count,
                Jsonb({"torch": torch.__version__, "botorch": version("botorch")}),
            ),
        )
        conn.commit()
    observations: list[Observation] = []
    excluded: set[str] = set()
    pareto: list[Observation] = []
    hypervolume = 0.0
    try:
        for iteration, candidate in enumerate(sobol_candidates(sobol_trials, seed)):
            recommendation_id = _persist_recommendation(
                settings,
                run_id,
                iteration,
                "SOBOL",
                candidate,
                context,
                3,
                seed,
                None,
                None,
                [],
            )
            observation = _evaluate_recommended_candidate(
                settings,
                campaign_id,
                "MO_SOBOL",
                iteration,
                seed,
                candidate,
                context,
                3,
                recommendation_id,
            )
            _link_recommendation(settings, recommendation_id, observation.observation_id)
            observations.append(observation)
            excluded.add(json.dumps(candidate.configuration, sort_keys=True))
            pareto, hypervolume = _persist_pareto_snapshot(
                settings, run_id, iteration, observations
            )
        for offset in range(bo_trials):
            observations = reconstruct_observations(settings, campaign_id)
            training_ids = [item.observation_id for item in observations]
            iteration = sobol_trials + offset
            recommendation_seed = seed + iteration
            candidate, value, probability = recommend_multiobjective(
                observations,
                recommendation_seed,
                excluded,
                acquisition_name,
                pool_size,
                sample_count,
            )
            recommendation_id = _persist_recommendation(
                settings,
                run_id,
                iteration,
                acquisition_name,
                candidate,
                context,
                3,
                recommendation_seed,
                value,
                probability,
                training_ids,
            )
            observation = _evaluate_recommended_candidate(
                settings,
                campaign_id,
                f"MO_{acquisition_name}",
                iteration,
                seed,
                candidate,
                context,
                3,
                recommendation_id,
                acquisition_name,
                value,
                probability,
                training_ids,
            )
            _link_recommendation(settings, recommendation_id, observation.observation_id)
            observations.append(observation)
            excluded.add(json.dumps(candidate.configuration, sort_keys=True))
            pareto, hypervolume = _persist_pareto_snapshot(
                settings, run_id, iteration, observations
            )
        selected = max(pareto, key=lambda item: (item.throughput_tps, -item.p99_ms))
        f4_observations: list[Observation] = []
        for replicate in range(f4_replicates):
            iteration = sobol_trials + bo_trials + replicate
            recommendation_id = _persist_recommendation(
                settings,
                run_id,
                iteration,
                "F4_VALIDATION",
                Candidate(selected.vector, selected.configuration),
                context,
                4,
                seed + replicate,
                None,
                None,
                [selected.observation_id],
            )
            observation = _evaluate_recommended_candidate(
                settings,
                campaign_id,
                "MO_F4_VALIDATION",
                replicate,
                seed + replicate,
                Candidate(selected.vector, selected.configuration),
                context,
                4,
                recommendation_id,
                training_ids=[selected.observation_id],
            )
            _link_recommendation(settings, recommendation_id, observation.observation_id)
            f4_observations.append(observation)
        champion_id, champion_status = _persist_champion(
            settings, run_id, selected, f4_observations, context_label
        )
        observations = reconstruct_observations(settings, campaign_id)
        status = "COMPLETED"
    except Exception:
        status = "FAILED"
        raise
    finally:
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.multi_objective_runs
                   SET status=%s,completed_at=clock_timestamp() WHERE run_id=%s""",
                (status, run_id),
            )
            cur.execute(
                """UPDATE charm_control.campaigns SET status=%s,updated_at=clock_timestamp()
                   WHERE campaign_id=%s""",
                (status, campaign_id),
            )
            conn.commit()
    return MultiObjectiveResult(
        run_id,
        campaign_id,
        tuple(observations),
        tuple(item.observation_id for item in pareto),
        hypervolume,
        champion_id,
        champion_status,
    )


def multiobjective_result_dict(result: MultiObjectiveResult) -> dict[str, Any]:
    return {
        "run_id": str(result.run_id),
        "campaign_id": str(result.campaign_id),
        "observation_ids": [str(item.observation_id) for item in result.observations],
        "pareto_observation_ids": [str(item) for item in result.pareto_observation_ids],
        "hypervolume": result.hypervolume,
        "champion_id": str(result.champion_id),
        "champion_status": result.champion_status,
        "evidence_warning": (
            "A CANDIDATE is one-context development evidence, not a robust thesis champion."
        ),
    }


def pareto_history(settings: Settings, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.snapshot_id,p.iteration,p.reference_point,p.feasible_observation_ids,
                   p.pareto_observation_ids,p.hypervolume,p.created_at
            FROM charm_control.pareto_snapshots p
            JOIN charm_control.multi_objective_runs r USING(run_id)
            WHERE r.campaign_id=%s ORDER BY p.iteration
            """,
            (campaign_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def champion_history(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.champion_id,c.run_id,c.selected_observation_id,o.configuration,c.rule,
                   c.f4_observation_ids,c.context_scores,c.robust_score,c.worst_p99_ms,c.status,
                   c.details,c.created_at
            FROM charm_control.champion_selections c
            JOIN charm_control.multi_objective_runs r USING(run_id)
            JOIN charm_control.optimization_observations o
              ON o.observation_id=c.selected_observation_id
            WHERE r.campaign_id=%s
            """,
            (campaign_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"campaign {campaign_id} has no champion selection")
    return dict(row)


def verify_recommendation(settings: Settings, recommendation_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.iteration,m.method,m.input_vector,m.configuration,m.pool_seed,
                   m.acquisition_value,m.training_observation_ids,r.campaign_id,
                   r.candidate_pool_size,r.sample_count,r.reference_point
            FROM charm_control.multi_objective_recommendations m
            JOIN charm_control.multi_objective_runs r USING(run_id)
            WHERE m.recommendation_id=%s
            """,
            (recommendation_id,),
        )
        recommendation = cur.fetchone()
        if recommendation is None:
            raise ValueError(f"unknown recommendation {recommendation_id}")
        training_ids = [
            uuid.UUID(str(value)) for value in recommendation["training_observation_ids"]
        ]
        cur.execute(
            """
            SELECT observation_id,input_vector,configuration,throughput_tps,p99_ms,failures,feasible
            FROM charm_control.optimization_observations
            WHERE observation_id=ANY(%s)
            """,
            (training_ids,),
        )
        by_id = {row["observation_id"]: row for row in cur.fetchall()}
    method = str(recommendation["method"])
    persisted_configuration = {
        str(key): str(value) for key, value in recommendation["configuration"].items()
    }
    if method == "SOBOL":
        candidates = sobol_candidates(
            int(recommendation["iteration"]) + 1, int(recommendation["pool_seed"])
        )
        reconstructed = candidates[int(recommendation["iteration"])]
        acquisition_value = None
    elif method in {"qLogNEHVI", "qLogNParEGO"}:
        observations = []
        for observation_id in training_ids:
            row = by_id.get(observation_id)
            if row is None:
                raise RuntimeError(f"missing training observation {observation_id}")
            observations.append(
                Observation(
                    observation_id,
                    tuple(float(value) for value in row["input_vector"]),  # type: ignore[arg-type]
                    {str(key): str(value) for key, value in row["configuration"].items()},
                    float(row["throughput_tps"]),
                    float(row["p99_ms"]),
                    int(row["failures"]),
                    bool(row["feasible"]),
                )
            )
        reconstructed, acquisition_value, _probability = recommend_multiobjective(
            observations,
            int(recommendation["pool_seed"]),
            {json.dumps(item.configuration, sort_keys=True) for item in observations},
            method,  # type: ignore[arg-type]
            int(recommendation["candidate_pool_size"]),
            int(recommendation["sample_count"]),
            tuple(float(value) for value in recommendation["reference_point"]),  # type: ignore[arg-type]
        )
    else:
        reconstructed = Candidate(
            tuple(float(value) for value in recommendation["input_vector"]),  # type: ignore[arg-type]
            persisted_configuration,
        )
        acquisition_value = None
    persisted_value = recommendation["acquisition_value"]
    value_matches = (
        persisted_value is None
        or acquisition_value is None
        or math.isclose(float(persisted_value), acquisition_value, rel_tol=1e-7, abs_tol=1e-9)
    )
    return {
        "recommendation_id": str(recommendation_id),
        "method": method,
        "persisted_configuration": persisted_configuration,
        "reconstructed_configuration": reconstructed.configuration,
        "configuration_matches": reconstructed.configuration == persisted_configuration,
        "acquisition_value_matches": value_matches,
        "verified": reconstructed.configuration == persisted_configuration and value_matches,
    }
