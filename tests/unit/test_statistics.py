import pytest

from charmdb.statistics import (
    cliffs_delta,
    descriptive_summary,
    holm_adjust,
    paired_comparison,
)


def test_descriptive_summary_is_seeded_and_uses_independent_values() -> None:
    first = descriptive_summary([10.0, 12.0, 14.0], bootstrap_samples=1000, seed=9)
    repeated = descriptive_summary([10.0, 12.0, 14.0], bootstrap_samples=1000, seed=9)

    assert first == repeated
    assert first.n == 3
    assert first.mean == 12
    assert first.median_absolute_deviation == 2


def test_paired_effect_and_permutation_are_campaign_level() -> None:
    comparison = paired_comparison([10.0, 20.0, 30.0], [12.0, 22.0, 32.0])

    assert comparison.n == 3
    assert comparison.mean_difference == 2
    assert comparison.standardized_paired_effect is None
    assert comparison.permutation_p_value == 0.25


def test_cliffs_delta_and_holm_adjustment() -> None:
    assert cliffs_delta([3.0, 4.0], [1.0, 2.0]) == 1.0
    assert holm_adjust([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
