import uuid
from dataclasses import replace

import pytest

from charmdb.calibration import (
    COVERAGE_LEVELS,
    MODEL_FAMILIES,
    CalibrationExample,
    Forecast,
    activation_gate,
    build_forecasts,
    label_forecasts,
    pair_features,
    score_forecasts,
)
from charmdb.workload import WorkloadResult


def _examples(count: int = 12) -> list[CalibrationExample]:
    examples: list[CalibrationExample] = []
    for index in range(count):
        value = index / max(1, count - 1)
        examples.append(
            CalibrationExample(
                uuid.uuid4(),
                (value, 1 - value, value / 2, 0.9 + value / 5, 0.7 + value / 5, 0.0),
                margin_ms=2.5 - index * 0.4,
                categorical_feasible=index not in {3, 9},
            )
        )
    return examples


def test_pair_features_keeps_configuration_and_f2_evidence_separate() -> None:
    result = WorkloadResult(100, 0, 450.0, 1.0, 2.0, 10.0, 5.0, 100)
    features = pair_features(
        {
            "random_page_cost": "2.5",
            "work_mem": "4096",
            "effective_io_concurrency": "50",
        },
        result,
        500.0,
    )
    assert features == pytest.approx((0.5, 0.4, 0.25, 0.9, 0.5, 0.0))


def test_surrogate_families_emit_ordered_prelabel_intervals_and_joint_rules() -> None:
    examples = _examples()
    forecasts = build_forecasts(examples, (0.3, 0.7, 0.2, 0.98, 0.65, 0.0))
    assert len(forecasts) == len(MODEL_FAMILIES) * len(COVERAGE_LEVELS)
    assert {item.model_family for item in forecasts} == set(MODEL_FAMILIES)
    assert all(item.observed_margin_ms is None for item in forecasts)
    for forecast in forecasts:
        assert forecast.lower_bound <= forecast.upper_bound
        assert 0 <= forecast.continuous_probability <= 1
        assert 0 <= forecast.categorical_probability <= 1
        assert forecast.selected_joint_rule == "minimum_marginal"
        assert forecast.selected_joint_probability == min(
            forecast.continuous_probability, forecast.categorical_probability
        )
        assert (
            forecast.joint_probabilities["union_bound"]
            <= forecast.joint_probabilities["minimum_marginal"]
        )


def test_false_safe_controlled_negative_blocks_activation() -> None:
    base = Forecast(
        model_family="gp",
        nominal_coverage=0.5,
        point_prediction=1.0,
        lower_bound=0.0,
        upper_bound=2.0,
        continuous_probability=0.99,
        categorical_probability=0.99,
        direct_joint_probability=0.99,
        joint_probabilities={
            "independence_product": 0.9801,
            "minimum_marginal": 0.99,
            "union_bound": 0.98,
            "direct_classifier": 0.99,
        },
        selected_joint_rule="minimum_marginal",
        selected_joint_probability=0.99,
        training_observation_ids=(),
        calibration_observation_ids=(),
    )
    forecasts: list[Forecast] = []
    for family in MODEL_FAMILIES:
        for sequence in range(12):
            observed = -1.0 if sequence in {0, 1, 2} else 1.0
            for level in COVERAGE_LEVELS:
                row = replace(
                    base,
                    model_family=family,
                    nominal_coverage=level,
                    lower_bound=-2.0,
                    upper_bound=2.0,
                    candidate_key=f"candidate-{sequence}",
                )
                forecasts.extend(label_forecasts([row], observed, True))
    metrics = score_forecasts(forecasts)
    assert metrics["gp"]["false_safe_count"] == 3
    enabled, selected, reasons = activation_gate(metrics, feasible_labels=9, infeasible_labels=3)
    assert not enabled
    assert selected in MODEL_FAMILIES
    assert any("false-safe" in reason for reason in reasons)


def test_activation_requires_both_feasibility_classes() -> None:
    metrics = {
        "gp": {
            "coverage": {
                "0.8": {"empirical_coverage": 0.8},
                "0.9": {"empirical_coverage": 0.9},
            },
            "brier_score": 0.01,
            "false_safe_count": 0,
        }
    }
    enabled, selected, reasons = activation_gate(metrics, feasible_labels=12, infeasible_labels=0)
    assert not enabled
    assert selected == "gp"
    assert any("each feasibility class" in reason for reason in reasons)
