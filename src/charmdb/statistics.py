from __future__ import annotations

import itertools
import math
import random
import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class DescriptiveSummary:
    n: int
    mean: float
    median: float
    standard_deviation: float
    median_absolute_deviation: float
    bootstrap_ci_95: tuple[float, float]


@dataclass(frozen=True)
class PairedComparison:
    n: int
    mean_difference: float
    median_difference: float
    standardized_paired_effect: float | None
    cliffs_delta: float
    permutation_p_value: float


def descriptive_summary(
    values: list[float], bootstrap_samples: int = 10_000, seed: int = 20260731
) -> DescriptiveSummary:
    if not values:
        raise ValueError("descriptive summary requires at least one independent value")
    if bootstrap_samples < 100:
        raise ValueError("bootstrap_samples must be at least 100")
    mean = statistics.fmean(values)
    median = statistics.median(values)
    standard_deviation = statistics.stdev(values) if len(values) > 1 else 0.0
    absolute_deviations = [abs(value - median) for value in values]
    mad = statistics.median(absolute_deviations)
    generator = random.Random(seed)
    bootstrap = sorted(
        statistics.fmean(generator.choices(values, k=len(values))) for _ in range(bootstrap_samples)
    )
    low_index = math.floor(0.025 * (bootstrap_samples - 1))
    high_index = math.ceil(0.975 * (bootstrap_samples - 1))
    return DescriptiveSummary(
        len(values),
        mean,
        median,
        standard_deviation,
        mad,
        (bootstrap[low_index], bootstrap[high_index]),
    )


def cliffs_delta(first: list[float], second: list[float]) -> float:
    if not first or not second:
        raise ValueError("Cliff's delta requires two non-empty samples")
    greater = sum(left > right for left in first for right in second)
    less = sum(left < right for left in first for right in second)
    return (greater - less) / (len(first) * len(second))


def paired_comparison(
    baseline: list[float],
    treatment: list[float],
    monte_carlo_samples: int = 100_000,
    seed: int = 20260731,
) -> PairedComparison:
    if len(baseline) != len(treatment) or len(baseline) < 2:
        raise ValueError("paired comparison requires equal samples with at least two blocks")
    differences = [right - left for left, right in zip(baseline, treatment, strict=True)]
    observed = abs(statistics.fmean(differences))
    standard_deviation = statistics.stdev(differences)
    standardized = (
        statistics.fmean(differences) / standard_deviation if standard_deviation else None
    )
    if len(differences) <= 20:
        signed_means = (
            abs(
                statistics.fmean(
                    sign * value for sign, value in zip(signs, differences, strict=True)
                )
            )
            for signs in itertools.product((-1, 1), repeat=len(differences))
        )
        extreme = sum(value >= observed - 1e-15 for value in signed_means)
        total = 2 ** len(differences)
        permutation_p_value = extreme / total
    else:
        if monte_carlo_samples < 1000:
            raise ValueError("monte_carlo_samples must be at least 1000")
        generator = random.Random(seed)
        extreme = 0
        for _ in range(monte_carlo_samples):
            value = abs(
                statistics.fmean(
                    generator.choice((-1, 1)) * difference for difference in differences
                )
            )
            extreme += value >= observed - 1e-15
        total = monte_carlo_samples
        permutation_p_value = (extreme + 1) / (total + 1)
    return PairedComparison(
        len(differences),
        statistics.fmean(differences),
        statistics.median(differences),
        standardized,
        cliffs_delta(treatment, baseline),
        permutation_p_value,
    )


def holm_adjust(p_values: list[float]) -> list[float]:
    if any(value < 0 or value > 1 for value in p_values):
        raise ValueError("p-values must be in [0, 1]")
    count = len(p_values)
    ordered = sorted(enumerate(p_values), key=lambda item: item[1])
    adjusted = [0.0] * count
    running = 0.0
    for rank, (index, value) in enumerate(ordered):
        running = max(running, min(1.0, (count - rank) * value))
        adjusted[index] = running
    return adjusted
