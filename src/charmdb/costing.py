from __future__ import annotations

from dataclasses import dataclass

COST_VARIANTS = ("no_cost", "benchmark_only", "restart_index_only", "complete")


@dataclass(frozen=True)
class OperationalCost:
    benchmark_seconds: float = 0.0
    restart_seconds: float = 0.0
    readiness_seconds: float = 0.0
    index_build_seconds: float = 0.0
    index_drop_seconds: float = 0.0
    index_build_wal_bytes: int = 0
    index_storage_bytes: int = 0
    configuration_churn: int = 0


@dataclass(frozen=True)
class CostReference:
    default_f3_seconds: float
    wal_bytes: int
    storage_bytes: int
    configuration_churn: int


@dataclass(frozen=True)
class NormalizedCost:
    benchmark: float
    restart_readiness: float
    index_time: float
    wal: float
    storage: float
    churn: float

    @property
    def total(self) -> float:
        return (
            self.benchmark
            + self.restart_readiness
            + self.index_time
            + self.wal
            + self.storage
            + self.churn
        )


def normalize_cost(cost: OperationalCost, reference: CostReference, variant: str) -> NormalizedCost:
    if variant not in COST_VARIANTS:
        raise ValueError(f"unsupported cost variant {variant}")
    if (
        reference.default_f3_seconds <= 0
        or reference.wal_bytes <= 0
        or reference.storage_bytes <= 0
        or reference.configuration_churn <= 0
    ):
        raise ValueError("cost normalization references must be positive")
    benchmark = cost.benchmark_seconds / reference.default_f3_seconds
    restart = (cost.restart_seconds + cost.readiness_seconds) / reference.default_f3_seconds
    index_time = (cost.index_build_seconds + cost.index_drop_seconds) / reference.default_f3_seconds
    wal = cost.index_build_wal_bytes / reference.wal_bytes
    storage = cost.index_storage_bytes / reference.storage_bytes
    churn = cost.configuration_churn / reference.configuration_churn
    if variant == "no_cost":
        return NormalizedCost(0, 0, 0, 0, 0, 0)
    if variant == "benchmark_only":
        return NormalizedCost(benchmark, 0, 0, 0, 0, 0)
    if variant == "restart_index_only":
        return NormalizedCost(0, restart, index_time, wal, storage, churn)
    return NormalizedCost(benchmark, restart, index_time, wal, storage, churn)


def cost_aware_utility(
    expected_improvement: float,
    probability_feasible: float,
    normalized_cost: NormalizedCost,
    alpha: float = 1.0,
    beta: float = 1.0,
) -> float:
    if expected_improvement < 0:
        raise ValueError("expected improvement cannot be negative")
    if not 0 <= probability_feasible <= 1:
        raise ValueError("feasibility probability must be in [0, 1]")
    if alpha < 0 or beta < 0:
        raise ValueError("cost/risk exponents cannot be negative")
    denominator = max(normalized_cost.total, 1e-9) ** alpha
    return float(expected_improvement * probability_feasible**beta / denominator)


def mandatory_f3_anchor(iteration: int, last_f3_iteration: int | None, interval: int) -> bool:
    if iteration < 0 or interval < 1:
        raise ValueError("invalid anchor schedule")
    return last_f3_iteration is None or iteration - last_f3_iteration >= interval


def unacceptable_performance_loss(
    cost_aware_best: float | None,
    no_cost_best: float | None,
    practical_margin: float,
) -> bool:
    if not 0 <= practical_margin < 1:
        raise ValueError("practical margin must be in [0, 1)")
    if no_cost_best is None:
        return False
    return cost_aware_best is None or cost_aware_best < no_cost_best * (1 - practical_margin)
