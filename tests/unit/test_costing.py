import pytest

from charmdb.costing import (
    CostReference,
    OperationalCost,
    cost_aware_utility,
    mandatory_f3_anchor,
    normalize_cost,
    unacceptable_performance_loss,
)


def test_cost_variants_keep_raw_dimensions_separate() -> None:
    cost = OperationalCost(30, 2, 1, 4, 1, 1000, 2000, 2)
    reference = CostReference(30, 1000, 2000, 2)
    no_cost = normalize_cost(cost, reference, "no_cost")
    benchmark = normalize_cost(cost, reference, "benchmark_only")
    operational = normalize_cost(cost, reference, "restart_index_only")
    complete = normalize_cost(cost, reference, "complete")
    assert no_cost.total == 0
    assert benchmark.total == 1
    assert operational.total == pytest.approx(3.2666666667)
    assert complete.total == pytest.approx(4.2666666667)


def test_cost_utility_and_mandatory_anchor_prevent_cheap_only_schedule() -> None:
    reference = CostReference(30, 1000, 2000, 2)
    cheap = normalize_cost(OperationalCost(5), reference, "complete")
    expensive = normalize_cost(OperationalCost(30), reference, "complete")
    assert cost_aware_utility(10, 0.9, cheap) > cost_aware_utility(10, 0.9, expensive)
    assert mandatory_f3_anchor(iteration=0, last_f3_iteration=None, interval=5)
    assert not mandatory_f3_anchor(iteration=4, last_f3_iteration=0, interval=5)
    assert mandatory_f3_anchor(iteration=5, last_f3_iteration=0, interval=5)


def test_cost_saving_with_practical_performance_loss_is_a_failure() -> None:
    assert unacceptable_performance_loss(470, 500, practical_margin=0.02)
    assert not unacceptable_performance_loss(495, 500, practical_margin=0.02)
