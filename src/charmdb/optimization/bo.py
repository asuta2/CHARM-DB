from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from typing import Literal

import torch
from botorch import fit_gpytorch_mll
from botorch.acquisition import AcquisitionFunction
from botorch.acquisition.logei import qLogNoisyExpectedImprovement
from botorch.acquisition.multi_objective.logei import qLogNoisyExpectedHypervolumeImprovement
from botorch.acquisition.multi_objective.objective import IdentityMCMultiOutputObjective
from botorch.acquisition.multi_objective.parego import qLogNParEGO
from botorch.models import ModelListGP, SingleTaskGP
from botorch.models.transforms.outcome import Standardize
from botorch.sampling.normal import SobolQMCNormalSampler
from botorch.utils.multi_objective.hypervolume import Hypervolume
from gpytorch.mlls.exact_marginal_log_likelihood import (  # type: ignore[import-untyped]
    ExactMarginalLogLikelihood,
)
from gpytorch.mlls.sum_marginal_log_likelihood import (  # type: ignore[import-untyped]
    SumMarginalLogLikelihood,
)

from charmdb.optimization.design import (
    PRIMARY_PARAMETER_ORDER,
    PrimaryCandidate,
    decode_primary_point,
)

DTYPE = torch.double
PRIMARY_REFERENCE_POINT = (0.0, -40.0)
PrimaryBOMethod = Literal[
    "bo_qlognei_throughput",
    "bo_qlognparego_multiobjective",
    "bo_qlognehvi_multiobjective",
]


@dataclass(frozen=True)
class PrimaryObservation:
    run_id: uuid.UUID
    vector: tuple[float, ...]
    configuration: dict[str, str]
    throughput_tps: float
    p99_ms: float
    failures: int
    hard_gates_passed: bool

    @property
    def valid(self) -> bool:
        return (
            self.hard_gates_passed
            and self.failures == 0
            and math.isfinite(self.throughput_tps)
            and self.throughput_tps > 0
            and math.isfinite(self.p99_ms)
            and self.p99_ms > 0
        )


@dataclass(frozen=True)
class PrimaryRecommendation:
    method: PrimaryBOMethod
    acquisition_name: str
    candidate: PrimaryCandidate
    acquisition_value: float
    training_run_ids: tuple[uuid.UUID, ...]


def _throughput_objective(samples: torch.Tensor, X: torch.Tensor | None = None) -> torch.Tensor:
    del X
    return samples[..., 0]


def primary_acquisition_name(method: str) -> str | None:
    names = {
        "random": None,
        "sobol": None,
        "bo_qlognei_throughput": "qLogNEI",
        "bo_qlognparego_multiobjective": "qLogNParEGO",
        "bo_qlognehvi_multiobjective": "qLogNEHVI",
    }
    if method not in names:
        raise ValueError(f"unsupported primary search method {method!r}")
    return names[method]


def primary_feasible_pareto(
    observations: list[PrimaryObservation],
) -> list[PrimaryObservation]:
    valid = [item for item in observations if item.valid]
    result: list[PrimaryObservation] = []
    for candidate in valid:
        dominated = any(
            other.run_id != candidate.run_id
            and other.throughput_tps >= candidate.throughput_tps
            and other.p99_ms <= candidate.p99_ms
            and (other.throughput_tps > candidate.throughput_tps or other.p99_ms < candidate.p99_ms)
            for other in valid
        )
        if not dominated:
            result.append(candidate)
    return sorted(result, key=lambda item: (item.throughput_tps, -item.p99_ms))


def primary_hypervolume(
    observations: list[PrimaryObservation],
    reference_point: tuple[float, float] = PRIMARY_REFERENCE_POINT,
) -> float:
    points = [
        [item.throughput_tps, -item.p99_ms]
        for item in primary_feasible_pareto(observations)
        if item.throughput_tps > reference_point[0] and -item.p99_ms > reference_point[1]
    ]
    if not points:
        return 0.0
    return float(
        Hypervolume(torch.tensor(reference_point, dtype=DTYPE)).compute(
            torch.tensor(points, dtype=DTYPE)
        )
    )


def _candidate_pool(seed: int, pool_size: int) -> torch.Tensor:
    return torch.quasirandom.SobolEngine(  # type: ignore[no-untyped-call]
        dimension=len(PRIMARY_PARAMETER_ORDER), scramble=True, seed=seed + 1000
    ).draw(pool_size, dtype=DTYPE)


def _select_candidate(
    acquisition: AcquisitionFunction,
    pool: torch.Tensor,
    payload: dict[str, object],
    excluded: set[str],
) -> tuple[PrimaryCandidate, float]:
    with torch.no_grad():
        values = acquisition(pool.unsqueeze(1)).reshape(-1)
        for index in torch.argsort(values, descending=True).tolist():
            candidate = decode_primary_point(pool[index].tolist(), payload)
            key = json.dumps(candidate.configuration, sort_keys=True, separators=(",", ":"))
            if key not in excluded:
                return candidate, float(values[index])
    raise RuntimeError("primary acquisition pool contained only duplicate configurations")


def recommend_primary_bo(
    method: PrimaryBOMethod,
    observations: list[PrimaryObservation],
    seed: int,
    excluded: set[str],
    payload: dict[str, object],
    *,
    pool_size: int = 2048,
    sample_count: int = 256,
    reference_point: tuple[float, float] = PRIMARY_REFERENCE_POINT,
) -> PrimaryRecommendation:
    acquisition_name = primary_acquisition_name(method)
    if acquisition_name is None:
        raise ValueError("a Bayesian primary method is required")
    if len(observations) < 4:
        raise ValueError("at least four valid isolated observations are required for GP fitting")
    if any(not item.valid for item in observations):
        raise ValueError("primary GP training data must contain only valid observations")
    if any(len(item.vector) != len(PRIMARY_PARAMETER_ORDER) for item in observations):
        raise ValueError("primary GP training vectors must use the frozen eight dimensions")
    if pool_size < 16 or sample_count < 16:
        raise ValueError("pool_size and sample_count must each be at least 16")
    if reference_point != PRIMARY_REFERENCE_POINT:
        raise ValueError("primary multi-objective acquisitions require reference point (0,-40)")

    torch.manual_seed(seed)
    train_x = torch.tensor([item.vector for item in observations], dtype=DTYPE)
    throughput = torch.tensor([[item.throughput_tps] for item in observations], dtype=DTYPE)
    sampler = SobolQMCNormalSampler(sample_shape=torch.Size([sample_count]), seed=seed)
    acquisition: AcquisitionFunction
    if method == "bo_qlognei_throughput":
        model = SingleTaskGP(train_x, throughput, outcome_transform=Standardize(m=1))
        fit_gpytorch_mll(ExactMarginalLogLikelihood(model.likelihood, model))
        acquisition = qLogNoisyExpectedImprovement(
            model=model,
            X_baseline=train_x,
            sampler=sampler,
            objective=None,
            constraints=None,
            prune_baseline=True,
        )
    else:
        negative_p99 = torch.tensor([[-item.p99_ms] for item in observations], dtype=DTYPE)
        throughput_gp = SingleTaskGP(train_x, throughput, outcome_transform=Standardize(m=1))
        latency_gp = SingleTaskGP(train_x, negative_p99, outcome_transform=Standardize(m=1))
        model = ModelListGP(throughput_gp, latency_gp)
        fit_gpytorch_mll(SumMarginalLogLikelihood(model.likelihood, model))
        objective = IdentityMCMultiOutputObjective(outcomes=[0, 1])
        if method == "bo_qlognehvi_multiobjective":
            acquisition = qLogNoisyExpectedHypervolumeImprovement(
                model=model,
                ref_point=list(reference_point),
                X_baseline=train_x,
                sampler=sampler,
                objective=objective,
                constraints=None,
                prune_baseline=True,
            )
        elif method == "bo_qlognparego_multiobjective":
            generator = torch.Generator().manual_seed(seed)
            weights = torch.rand(2, dtype=DTYPE, generator=generator)
            weights /= weights.sum()
            acquisition = qLogNParEGO(
                model=model,
                X_baseline=train_x,
                scalarization_weights=weights,
                sampler=sampler,
                objective=objective,
                constraints=None,
                prune_baseline=True,
            )
        else:
            raise ValueError(f"unsupported primary BO method {method!r}")

    candidate, value = _select_candidate(
        acquisition, _candidate_pool(seed, pool_size), payload, excluded
    )
    return PrimaryRecommendation(
        method=method,
        acquisition_name=acquisition_name,
        candidate=candidate,
        acquisition_value=value,
        training_run_ids=tuple(item.run_id for item in observations),
    )
