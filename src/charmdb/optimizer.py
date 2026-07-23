from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass

import torch
from botorch import fit_gpytorch_mll
from botorch.acquisition.logei import qLogNoisyExpectedImprovement
from botorch.acquisition.objective import GenericMCObjective
from botorch.models import ModelListGP, SingleTaskGP
from botorch.models.transforms.outcome import Standardize
from botorch.sampling.normal import SobolQMCNormalSampler
from gpytorch.mlls.sum_marginal_log_likelihood import (  # type: ignore[import-untyped]
    SumMarginalLogLikelihood,
)
from psycopg.types.json import Jsonb
from torch import Tensor

from charmdb.config import Settings
from charmdb.controller import apply_configuration, rollback_configuration
from charmdb.db import connect
from charmdb.resources import capture_and_validate_resources
from charmdb.workload import WorkloadResult, benchmark_default

DTYPE = torch.double
P99_SLO_MS = 20.0


def _throughput_objective(samples: Tensor, X: Tensor | None = None) -> Tensor:
    del X
    return samples[..., 0]


def _p99_constraint(samples: Tensor) -> Tensor:
    return samples[..., 1]


@dataclass(frozen=True)
class Candidate:
    vector: tuple[float, float, float]
    configuration: dict[str, str]


@dataclass(frozen=True)
class Observation:
    observation_id: uuid.UUID
    vector: tuple[float, float, float]
    configuration: dict[str, str]
    throughput_tps: float
    p99_ms: float
    failures: int
    feasible: bool


def decode_vector(vector: Tensor) -> Candidate:
    values = vector.detach().cpu().tolist()
    if len(values) != 3 or any(value < 0 or value > 1 for value in values):
        raise ValueError("normalized candidate must contain three values in [0, 1]")
    random_page_cost = round(1.0 + values[0] * 3.0, 2)
    work_mem_power = round(10 + values[1] * 5)
    effective_io_concurrency = round(values[2] * 200)
    return Candidate(
        vector=(float(values[0]), float(values[1]), float(values[2])),
        configuration={
            "random_page_cost": f"{random_page_cost:g}",
            "work_mem": str(2**work_mem_power),
            "effective_io_concurrency": str(effective_io_concurrency),
        },
    )


def encode_configuration(configuration: dict[str, str]) -> tuple[float, float, float]:
    return (
        (float(configuration["random_page_cost"]) - 1.0) / 3.0,
        (math.log2(int(configuration["work_mem"])) - 10.0) / 5.0,
        int(configuration["effective_io_concurrency"]) / 200.0,
    )


def sobol_candidates(count: int, seed: int) -> list[Candidate]:
    if count < 1:
        raise ValueError("count must be positive")
    raw = torch.quasirandom.SobolEngine(  # type: ignore[no-untyped-call]
        3, scramble=True, seed=seed
    ).draw(count, dtype=DTYPE)
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for row in raw:
        candidate = decode_vector(row)
        key = json.dumps(candidate.configuration, sort_keys=True)
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)
    return candidates


def recommend_constrained_bo(
    observations: list[Observation],
    seed: int,
    excluded: set[str],
    pool_size: int = 2048,
    sample_count: int = 256,
) -> tuple[Candidate, float, float]:
    if len(observations) < 4:
        raise ValueError("at least four observations are required for GP fitting")
    train_x = torch.tensor([item.vector for item in observations], dtype=DTYPE)
    throughput = torch.tensor([[item.throughput_tps] for item in observations], dtype=DTYPE)
    constraint = torch.tensor([[item.p99_ms - P99_SLO_MS] for item in observations], dtype=DTYPE)
    objective_gp = SingleTaskGP(train_x, throughput, outcome_transform=Standardize(m=1))
    constraint_gp = SingleTaskGP(train_x, constraint, outcome_transform=Standardize(m=1))
    model = ModelListGP(objective_gp, constraint_gp)
    fit_gpytorch_mll(SumMarginalLogLikelihood(model.likelihood, model))
    acquisition = qLogNoisyExpectedImprovement(
        model=model,
        X_baseline=train_x,
        sampler=SobolQMCNormalSampler(sample_shape=torch.Size([sample_count]), seed=seed),
        objective=GenericMCObjective(_throughput_objective),
        constraints=[_p99_constraint],
        prune_baseline=True,
    )
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


def persist_observation(
    settings: Settings,
    campaign_id: uuid.UUID,
    method: str,
    iteration: int,
    seed: int,
    candidate: Candidate,
    result: WorkloadResult,
    benchmark_trial_id: uuid.UUID,
    application_id: uuid.UUID,
    acquisition_name: str | None = None,
    acquisition_value: float | None = None,
    probability_feasible: float | None = None,
    training_ids: list[uuid.UUID] | None = None,
    workload_context: dict[str, object] | None = None,
    fidelity: int = 3,
    recommendation_id: uuid.UUID | None = None,
    diagnostic_details: dict[str, object] | None = None,
) -> Observation:
    observation_id = uuid.uuid4()
    feasible = result.failures == 0 and result.p99_ms <= P99_SLO_MS
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.optimization_observations
            (observation_id,campaign_id,benchmark_trial_id,application_id,method,iteration,
             random_seed,input_vector,configuration,throughput_tps,p99_ms,p99_margin_ms,
             failures,feasible,acquisition_name,acquisition_value,probability_feasible,
             training_observation_ids,status,workload_context,fidelity,recommendation_id,
             diagnostic_details)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                observation_id,
                campaign_id,
                benchmark_trial_id,
                application_id,
                method,
                iteration,
                seed,
                Jsonb(candidate.vector),
                Jsonb(candidate.configuration),
                result.throughput_tps,
                result.p99_ms,
                P99_SLO_MS - result.p99_ms,
                result.failures,
                feasible,
                acquisition_name,
                acquisition_value,
                probability_feasible,
                Jsonb([str(item) for item in training_ids or []]),
                "COMPLETED" if feasible else "INFEASIBLE",
                Jsonb(workload_context or {}),
                fidelity,
                recommendation_id,
                Jsonb(diagnostic_details or {}),
            ),
        )
        conn.commit()
    return Observation(
        observation_id,
        candidate.vector,
        candidate.configuration,
        result.throughput_tps,
        result.p99_ms,
        result.failures,
        feasible,
    )


def evaluate_candidate(
    settings: Settings,
    campaign_id: uuid.UUID,
    method: str,
    iteration: int,
    seed: int,
    candidate: Candidate,
    acquisition_name: str | None = None,
    acquisition_value: float | None = None,
    probability_feasible: float | None = None,
    training_ids: list[uuid.UUID] | None = None,
    workload_context: dict[str, object] | None = None,
    fidelity: int = 3,
    recommendation_id: uuid.UUID | None = None,
) -> Observation:
    resources_before = capture_and_validate_resources(settings)
    application = apply_configuration(settings, candidate.configuration)
    try:
        benchmark_settings = settings.model_copy(update={"benchmark_seed": seed})
        trial_id, _path, result = benchmark_default(
            benchmark_settings,
            active_configuration=candidate.configuration,
            label=method.lower(),
            fidelity=fidelity,
        )
        resources_after = capture_and_validate_resources(settings)
        return persist_observation(
            settings,
            campaign_id,
            method,
            iteration,
            seed,
            candidate,
            result,
            trial_id,
            application.application_id,
            acquisition_name,
            acquisition_value,
            probability_feasible,
            training_ids,
            workload_context,
            fidelity,
            recommendation_id,
            {"resources_before": resources_before, "resources_after": resources_after},
        )
    finally:
        rollback_configuration(
            settings, application.application_id, f"restore after {method} iteration {iteration}"
        )


def run_single_objective_campaign(
    settings: Settings, sobol_trials: int, bo_trials: int, seed: int
) -> tuple[uuid.UUID, list[Observation]]:
    if sobol_trials < 4 or bo_trials < 1:
        raise ValueError("campaign requires at least four Sobol and one BO trial")
    campaign_id = uuid.uuid4()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.campaigns
            (campaign_id,name,mode,status,objective_definition,constraint_definition)
            VALUES (%s,%s,'CONSTRAINED_SINGLE','RUNNING',%s,%s)""",
            (
                campaign_id,
                f"constrained-single-seed-{seed}",
                Jsonb({"maximize": "throughput_tps"}),
                Jsonb({"p99_ms_max": P99_SLO_MS, "failures_max": 0}),
            ),
        )
        conn.commit()
    observations: list[Observation] = []
    excluded: set[str] = set()
    try:
        for iteration, candidate in enumerate(sobol_candidates(sobol_trials, seed)):
            observation = evaluate_candidate(
                settings, campaign_id, "SOBOL", iteration, seed, candidate
            )
            observations.append(observation)
            excluded.add(json.dumps(candidate.configuration, sort_keys=True))
        for offset in range(bo_trials):
            training_ids = [item.observation_id for item in observations]
            candidate, value, probability = recommend_constrained_bo(
                observations, seed + offset, excluded
            )
            observation = evaluate_candidate(
                settings,
                campaign_id,
                "CONSTRAINED_BO",
                sobol_trials + offset,
                seed,
                candidate,
                "qLogNEI",
                value,
                probability,
                training_ids,
            )
            observations.append(observation)
            excluded.add(json.dumps(candidate.configuration, sort_keys=True))
        status = "COMPLETED"
    except Exception:
        status = "FAILED"
        raise
    finally:
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.campaigns
                SET status=%s,updated_at=clock_timestamp() WHERE campaign_id=%s""",
                (status, campaign_id),
            )
            conn.commit()
    return campaign_id, observations


def observations_json(campaign_id: uuid.UUID, observations: list[Observation]) -> str:
    return json.dumps(
        {
            "campaign_id": str(campaign_id),
            "observations": [
                {
                    "observation_id": str(item.observation_id),
                    "configuration": item.configuration,
                    "throughput_tps": item.throughput_tps,
                    "p99_ms": item.p99_ms,
                    "failures": item.failures,
                    "feasible": item.feasible,
                }
                for item in observations
            ],
        },
        indent=2,
    )
