from __future__ import annotations

import json
import math
import statistics
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
from numpy.typing import NDArray
from psycopg.types.json import Jsonb
from scipy.stats import norm  # type: ignore[import-untyped]
from sklearn.ensemble import (  # type: ignore[import-untyped]
    GradientBoostingRegressor,
    RandomForestRegressor,
)
from sklearn.gaussian_process import GaussianProcessRegressor  # type: ignore[import-untyped]
from sklearn.gaussian_process.kernels import (  # type: ignore[import-untyped]
    ConstantKernel,
    Matern,
    WhiteKernel,
)
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]

from charmdb.config import Settings
from charmdb.controller import (
    apply_configuration,
    discover_knobs,
    rollback_configuration,
    validate_candidate,
)
from charmdb.db import connect
from charmdb.fidelity import _baseline_tps, _candidate_key, _persist_fidelity
from charmdb.optimizer import P99_SLO_MS, Candidate, encode_configuration, sobol_candidates
from charmdb.workload import WorkloadResult, benchmark_default

COVERAGE_LEVELS: tuple[float, ...] = (0.5, 0.8, 0.9, 0.95)
MODEL_FAMILIES = (
    "gp",
    "random_forest",
    "gradient_boosted_quantile",
    "split_cqr",
    "rolling_conformal",
)
MINIMUM_TRAINING_EXAMPLES = 8
PROMOTION_PROBABILITY_THRESHOLD = 0.95


@dataclass(frozen=True)
class CalibrationExample:
    observation_id: uuid.UUID
    features: tuple[float, ...]
    margin_ms: float
    categorical_feasible: bool

    @property
    def joint_feasible(self) -> bool:
        return self.margin_ms >= 0 and self.categorical_feasible


@dataclass(frozen=True)
class Forecast:
    model_family: str
    nominal_coverage: float
    point_prediction: float
    lower_bound: float
    upper_bound: float
    continuous_probability: float
    categorical_probability: float
    direct_joint_probability: float | None
    joint_probabilities: dict[str, float | None]
    selected_joint_rule: str
    selected_joint_probability: float
    training_observation_ids: tuple[uuid.UUID, ...]
    calibration_observation_ids: tuple[uuid.UUID, ...]
    prediction_id: uuid.UUID | None = None
    candidate_key: str | None = None
    sequence_number: int | None = None
    observed_margin_ms: float | None = None
    observed_categorical_feasible: bool | None = None

    @property
    def observed_joint_feasible(self) -> bool | None:
        if self.observed_margin_ms is None or self.observed_categorical_feasible is None:
            return None
        return self.observed_margin_ms >= 0 and self.observed_categorical_feasible


@dataclass(frozen=True)
class CalibrationPilotResult:
    campaign_id: uuid.UUID
    paired_candidates: int
    scored_candidates: int
    feasible_labels: int
    infeasible_labels: int
    probabilistic_promotion_enabled: bool
    selected_model: str | None
    activation_reasons: tuple[str, ...]
    wall_clock_seconds: float


def pair_features(
    configuration: dict[str, str], f2: WorkloadResult, baseline_tps: float
) -> tuple[float, ...]:
    encoded = encode_configuration(configuration)
    return (
        *encoded,
        f2.throughput_tps / baseline_tps,
        f2.p99_ms / P99_SLO_MS,
        min(float(f2.failures), 1.0),
    )


def _arrays(
    examples: list[CalibrationExample],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    return (
        np.asarray([item.features for item in examples], dtype=np.float64),
        np.asarray([item.margin_ms for item in examples], dtype=np.float64),
    )


def _normal_probability(mean: float, scale: float) -> float:
    if scale <= 1e-12:
        return float(mean >= 0)
    return float(norm.cdf(mean / scale))


def _smoothed_binary_probability(labels: list[bool]) -> float:
    return (sum(labels) + 1.0) / (len(labels) + 2.0)


def _classifier_probability(
    examples: list[CalibrationExample], features: tuple[float, ...], target: str
) -> float | None:
    if target == "categorical":
        labels = [item.categorical_feasible for item in examples]
    elif target == "joint":
        labels = [item.joint_feasible for item in examples]
    else:
        raise ValueError(f"unknown classification target {target}")
    if len(set(labels)) < 2:
        return None
    train_x, _ = _arrays(examples)
    model = LogisticRegression(random_state=0, class_weight="balanced", max_iter=1000)
    model.fit(train_x, np.asarray(labels, dtype=np.int64))
    probabilities = model.predict_proba(np.asarray([features], dtype=np.float64))[0]
    positive_index = list(model.classes_).index(1)
    return float(probabilities[positive_index])


def _risk_probabilities(
    examples: list[CalibrationExample],
    features: tuple[float, ...],
    continuous_probability: float,
) -> tuple[float, float | None, dict[str, float | None], float]:
    categorical_labels = [item.categorical_feasible for item in examples]
    categorical = _classifier_probability(examples, features, "categorical")
    if categorical is None:
        categorical = _smoothed_binary_probability(categorical_labels)
    direct = _classifier_probability(examples, features, "joint")
    joint: dict[str, float | None] = {
        "independence_product": continuous_probability * categorical,
        "minimum_marginal": min(continuous_probability, categorical),
        "union_bound": max(0.0, continuous_probability + categorical - 1.0),
        "direct_classifier": direct,
    }
    selected = min(continuous_probability, categorical)
    return categorical, direct, joint, selected


def _base_forecast(
    model_family: str,
    level: float,
    point: float,
    lower: float,
    upper: float,
    continuous_probability: float,
    examples: list[CalibrationExample],
    features: tuple[float, ...],
    calibration_ids: tuple[uuid.UUID, ...] = (),
) -> Forecast:
    categorical, direct, joint, selected = _risk_probabilities(
        examples, features, continuous_probability
    )
    return Forecast(
        model_family=model_family,
        nominal_coverage=level,
        point_prediction=point,
        lower_bound=min(lower, upper),
        upper_bound=max(lower, upper),
        continuous_probability=continuous_probability,
        categorical_probability=categorical,
        direct_joint_probability=direct,
        joint_probabilities=joint,
        selected_joint_rule="minimum_marginal",
        selected_joint_probability=selected,
        training_observation_ids=tuple(item.observation_id for item in examples),
        calibration_observation_ids=calibration_ids,
    )


def _gp_forecasts(
    examples: list[CalibrationExample], features: tuple[float, ...]
) -> list[Forecast]:
    train_x, train_y = _arrays(examples)
    kernel = ConstantKernel(1.0) * Matern(length_scale=np.ones(train_x.shape[1]), nu=2.5)
    kernel += WhiteKernel(noise_level=0.05)
    model = GaussianProcessRegressor(
        kernel=kernel, normalize_y=True, optimizer=None, random_state=0
    )
    model.fit(train_x, train_y)
    means, standard_deviations = model.predict(
        np.asarray([features], dtype=np.float64), return_std=True
    )
    point = float(means[0])
    scale = max(float(standard_deviations[0]), 1e-9)
    probability = _normal_probability(point, scale)
    return [
        _base_forecast(
            "gp",
            level,
            point,
            point - float(norm.ppf((1 + level) / 2)) * scale,
            point + float(norm.ppf((1 + level) / 2)) * scale,
            probability,
            examples,
            features,
        )
        for level in COVERAGE_LEVELS
    ]


def _random_forest_forecasts(
    examples: list[CalibrationExample], features: tuple[float, ...]
) -> list[Forecast]:
    train_x, train_y = _arrays(examples)
    model = RandomForestRegressor(
        n_estimators=128,
        min_samples_leaf=2,
        max_features=1.0,
        random_state=0,
        n_jobs=1,
    )
    model.fit(train_x, train_y)
    sample = np.asarray([features], dtype=np.float64)
    tree_predictions = np.asarray(
        [float(estimator.predict(sample)[0]) for estimator in model.estimators_],
        dtype=np.float64,
    )
    point = float(np.median(tree_predictions))
    probability = (float(np.sum(tree_predictions >= 0)) + 1.0) / (len(tree_predictions) + 2.0)
    return [
        _base_forecast(
            "random_forest",
            level,
            point,
            float(np.quantile(tree_predictions, (1 - level) / 2)),
            float(np.quantile(tree_predictions, 1 - (1 - level) / 2)),
            probability,
            examples,
            features,
        )
        for level in COVERAGE_LEVELS
    ]


def _gradient_boosted_forecasts(
    examples: list[CalibrationExample], features: tuple[float, ...]
) -> list[Forecast]:
    train_x, train_y = _arrays(examples)
    sample = np.asarray([features], dtype=np.float64)
    point_model = GradientBoostingRegressor(
        loss="huber", n_estimators=80, max_depth=2, min_samples_leaf=2, random_state=0
    )
    point_model.fit(train_x, train_y)
    point = float(point_model.predict(sample)[0])
    residual_scale = max(float(np.std(train_y - point_model.predict(train_x), ddof=1)), 1e-9)
    probability = _normal_probability(point, residual_scale)
    forecasts: list[Forecast] = []
    for level in COVERAGE_LEVELS:
        tail = (1 - level) / 2
        lower_model = GradientBoostingRegressor(
            loss="quantile",
            alpha=tail,
            n_estimators=80,
            max_depth=2,
            min_samples_leaf=2,
            random_state=0,
        )
        upper_model = GradientBoostingRegressor(
            loss="quantile",
            alpha=1 - tail,
            n_estimators=80,
            max_depth=2,
            min_samples_leaf=2,
            random_state=0,
        )
        lower_model.fit(train_x, train_y)
        upper_model.fit(train_x, train_y)
        forecasts.append(
            _base_forecast(
                "gradient_boosted_quantile",
                level,
                point,
                float(lower_model.predict(sample)[0]),
                float(upper_model.predict(sample)[0]),
                probability,
                examples,
                features,
            )
        )
    return forecasts


def _conformal_quantile(scores: NDArray[np.float64], level: float) -> float:
    rank = min(math.ceil((len(scores) + 1) * level), len(scores))
    return float(np.sort(scores)[rank - 1])


def _split_cqr_forecasts(
    examples: list[CalibrationExample], features: tuple[float, ...]
) -> list[Forecast]:
    split = max(4, len(examples) // 2)
    fit_examples = examples[:split]
    calibration_examples = examples[split:]
    fit_x, fit_y = _arrays(fit_examples)
    calibration_x, calibration_y = _arrays(calibration_examples)
    sample = np.asarray([features], dtype=np.float64)
    median_model = GradientBoostingRegressor(
        loss="quantile",
        alpha=0.5,
        n_estimators=80,
        max_depth=2,
        min_samples_leaf=2,
        random_state=0,
    )
    median_model.fit(fit_x, fit_y)
    point = float(median_model.predict(sample)[0])
    median_residuals = calibration_y - median_model.predict(calibration_x)
    residual_scale = max(float(np.std(median_residuals, ddof=1)), 1e-9)
    probability = _normal_probability(point, residual_scale)
    forecasts: list[Forecast] = []
    calibration_ids = tuple(item.observation_id for item in calibration_examples)
    for level in COVERAGE_LEVELS:
        tail = (1 - level) / 2
        lower_model = GradientBoostingRegressor(
            loss="quantile",
            alpha=tail,
            n_estimators=80,
            max_depth=2,
            min_samples_leaf=2,
            random_state=0,
        )
        upper_model = GradientBoostingRegressor(
            loss="quantile",
            alpha=1 - tail,
            n_estimators=80,
            max_depth=2,
            min_samples_leaf=2,
            random_state=0,
        )
        lower_model.fit(fit_x, fit_y)
        upper_model.fit(fit_x, fit_y)
        calibration_lower = lower_model.predict(calibration_x)
        calibration_upper = upper_model.predict(calibration_x)
        conformity = np.maximum(
            calibration_lower - calibration_y, calibration_y - calibration_upper
        )
        adjustment = max(0.0, _conformal_quantile(conformity, level))
        forecasts.append(
            _base_forecast(
                "split_cqr",
                level,
                point,
                float(lower_model.predict(sample)[0]) - adjustment,
                float(upper_model.predict(sample)[0]) + adjustment,
                probability,
                examples,
                features,
                calibration_ids,
            )
        )
    return forecasts


def _rolling_conformal_forecasts(
    examples: list[CalibrationExample], features: tuple[float, ...]
) -> list[Forecast]:
    calibration_count = min(8, max(4, len(examples) // 3))
    fit_examples = examples[:-calibration_count]
    calibration_examples = examples[-calibration_count:]
    fit_x, fit_y = _arrays(fit_examples)
    calibration_x, calibration_y = _arrays(calibration_examples)
    sample = np.asarray([features], dtype=np.float64)
    model = RandomForestRegressor(
        n_estimators=128,
        min_samples_leaf=2,
        max_features=1.0,
        random_state=0,
        n_jobs=1,
    )
    model.fit(fit_x, fit_y)
    point = float(model.predict(sample)[0])
    residuals = calibration_y - model.predict(calibration_x)
    absolute_residuals = np.abs(residuals)
    residual_scale = max(float(np.std(residuals, ddof=1)), 1e-9)
    probability = _normal_probability(point, residual_scale)
    calibration_ids = tuple(item.observation_id for item in calibration_examples)
    return [
        _base_forecast(
            "rolling_conformal",
            level,
            point,
            point - _conformal_quantile(absolute_residuals, level),
            point + _conformal_quantile(absolute_residuals, level),
            probability,
            examples,
            features,
            calibration_ids,
        )
        for level in COVERAGE_LEVELS
    ]


def build_forecasts(
    examples: list[CalibrationExample], features: tuple[float, ...]
) -> list[Forecast]:
    if len(examples) < MINIMUM_TRAINING_EXAMPLES:
        raise ValueError(
            f"at least {MINIMUM_TRAINING_EXAMPLES} prior examples are required for comparison"
        )
    return [
        *_gp_forecasts(examples, features),
        *_random_forest_forecasts(examples, features),
        *_gradient_boosted_forecasts(examples, features),
        *_split_cqr_forecasts(examples, features),
        *_rolling_conformal_forecasts(examples, features),
    ]


def label_forecasts(
    forecasts: list[Forecast], margin_ms: float, categorical_feasible: bool
) -> list[Forecast]:
    return [
        replace(
            item,
            observed_margin_ms=margin_ms,
            observed_categorical_feasible=categorical_feasible,
        )
        for item in forecasts
    ]


def _observed_margin(forecast: Forecast) -> float:
    if forecast.observed_margin_ms is None:
        raise ValueError("forecast has not been labeled")
    return forecast.observed_margin_ms


def _ece(probabilities: list[float], labels: list[bool], bins: int = 5) -> float:
    if not probabilities:
        return math.nan
    total = len(probabilities)
    error = 0.0
    for index in range(bins):
        low = index / bins
        high = (index + 1) / bins
        members = [
            item
            for item, probability in enumerate(probabilities)
            if (low <= probability <= high if index == bins - 1 else low <= probability < high)
        ]
        if members:
            confidence = statistics.mean(probabilities[item] for item in members)
            accuracy = statistics.mean(float(labels[item]) for item in members)
            error += len(members) / total * abs(confidence - accuracy)
    return error


def _probability_metrics(probabilities: list[float], labels: list[bool]) -> dict[str, Any]:
    false_safe = [
        probability >= PROMOTION_PROBABILITY_THRESHOLD and not label
        for probability, label in zip(probabilities, labels, strict=True)
    ]
    predicted_safe = [
        probability >= PROMOTION_PROBABILITY_THRESHOLD for probability in probabilities
    ]
    return {
        "sample_size": len(probabilities),
        "brier_score": statistics.mean(
            (probability - float(label)) ** 2
            for probability, label in zip(probabilities, labels, strict=True)
        ),
        "expected_calibration_error": _ece(probabilities, labels),
        "false_safe_count": sum(false_safe),
        "predicted_safe_count": sum(predicted_safe),
        "false_safe_rate": sum(false_safe) / max(1, sum(predicted_safe)),
        "reliability": _reliability(probabilities, labels),
    }


def _time_coverage(rows: list[Forecast]) -> list[dict[str, float | int | str]]:
    ordered = sorted(rows, key=lambda item: item.sequence_number or -1)
    split = max(1, len(ordered) // 2)
    result: list[dict[str, float | int | str]] = []
    for label, block in (("early", ordered[:split]), ("late", ordered[split:])):
        if block:
            hits = [
                item.lower_bound <= _observed_margin(item) <= item.upper_bound for item in block
            ]
            result.append(
                {
                    "period": label,
                    "sample_size": len(block),
                    "empirical_coverage": statistics.mean(hits),
                }
            )
    return result


def score_forecasts(forecasts: list[Forecast]) -> dict[str, Any]:
    labeled = [item for item in forecasts if item.observed_joint_feasible is not None]
    metrics: dict[str, Any] = {}
    for family in MODEL_FAMILIES:
        family_rows = [item for item in labeled if item.model_family == family]
        if not family_rows:
            continue
        coverage: dict[str, Any] = {}
        for level in COVERAGE_LEVELS:
            rows = [item for item in family_rows if item.nominal_coverage == level]
            hits = [item.lower_bound <= _observed_margin(item) <= item.upper_bound for item in rows]
            coverage[str(level)] = {
                "sample_size": len(rows),
                "empirical_coverage": statistics.mean(hits) if hits else math.nan,
                "mean_interval_width_ms": (
                    statistics.mean(item.upper_bound - item.lower_bound for item in rows)
                    if rows
                    else math.nan
                ),
            }
        probability_rows = [item for item in family_rows if item.nominal_coverage == 0.5]
        continuous_labels = [_observed_margin(item) >= 0 for item in probability_rows]
        categorical_labels = [bool(item.observed_categorical_feasible) for item in probability_rows]
        joint_labels = [bool(item.observed_joint_feasible) for item in probability_rows]
        continuous_metrics = _probability_metrics(
            [item.continuous_probability for item in probability_rows], continuous_labels
        )
        categorical_metrics = _probability_metrics(
            [item.categorical_probability for item in probability_rows], categorical_labels
        )
        joint_rule_metrics: dict[str, Any] = {}
        for rule in (
            "independence_product",
            "minimum_marginal",
            "union_bound",
            "direct_classifier",
        ):
            available = [
                (value, label)
                for item, label in zip(probability_rows, joint_labels, strict=True)
                if (value := item.joint_probabilities[rule]) is not None
            ]
            if available:
                joint_rule_metrics[rule] = _probability_metrics(
                    [item[0] for item in available], [item[1] for item in available]
                )
        selected_metrics = joint_rule_metrics["minimum_marginal"]
        residuals = [_observed_margin(item) - item.point_prediction for item in probability_rows]
        eighty_rows = [item for item in family_rows if item.nominal_coverage == 0.8]
        z_value = float(norm.ppf(0.9))
        standardized_residuals = [
            (_observed_margin(item) - item.point_prediction)
            / max((item.upper_bound - item.lower_bound) / (2 * z_value), 1e-9)
            for item in eighty_rows
        ]
        gaussian_nlpd: float | None = None
        if family == "gp":
            gaussian_nlpd = statistics.mean(
                0.5
                * math.log(
                    2
                    * math.pi
                    * max((item.upper_bound - item.lower_bound) / (2 * z_value), 1e-9) ** 2
                )
                + 0.5
                * (
                    (_observed_margin(item) - item.point_prediction)
                    / max((item.upper_bound - item.lower_bound) / (2 * z_value), 1e-9)
                )
                ** 2
                for item in eighty_rows
            )
        metrics[family] = {
            "coverage": coverage,
            "mean_residual_ms": statistics.mean(residuals) if residuals else math.nan,
            "mean_standardized_residual": statistics.mean(standardized_residuals),
            "gaussian_negative_log_predictive_density": gaussian_nlpd,
            "continuous_margin_probability": continuous_metrics,
            "categorical_feasibility_probability": categorical_metrics,
            "joint_rule_scores": joint_rule_metrics,
            "brier_score": selected_metrics["brier_score"],
            "expected_calibration_error": selected_metrics["expected_calibration_error"],
            "false_safe_count": selected_metrics["false_safe_count"],
            "predicted_safe_count": selected_metrics["predicted_safe_count"],
            "false_safe_rate": selected_metrics["false_safe_rate"],
            "reliability": selected_metrics["reliability"],
            "strata": {
                "fidelity": "F3 runtime label",
                "context": "development pgbench",
                "prequential_time_90": _time_coverage(
                    [item for item in family_rows if item.nominal_coverage == 0.9]
                ),
            },
        }
    return metrics


def _reliability(
    probabilities: list[float], labels: list[bool], bins: int = 5
) -> list[dict[str, float | int]]:
    result: list[dict[str, float | int]] = []
    for index in range(bins):
        low = index / bins
        high = (index + 1) / bins
        members = [
            item
            for item, probability in enumerate(probabilities)
            if (low <= probability <= high if index == bins - 1 else low <= probability < high)
        ]
        if members:
            result.append(
                {
                    "lower": low,
                    "upper": high,
                    "count": len(members),
                    "mean_probability": statistics.mean(probabilities[item] for item in members),
                    "observed_frequency": statistics.mean(float(labels[item]) for item in members),
                }
            )
    return result


def activation_gate(
    metrics: dict[str, Any], feasible_labels: int, infeasible_labels: int
) -> tuple[bool, str | None, tuple[str, ...]]:
    reasons: list[str] = []
    scored = feasible_labels + infeasible_labels
    if scored < 12:
        reasons.append("fewer than 12 prequentially scored candidates")
    if feasible_labels < 3 or infeasible_labels < 3:
        reasons.append("fewer than three labels in each feasibility class")
    if not metrics:
        reasons.append("no model metrics are available")
        return False, None, tuple(reasons)

    def ranking(item: tuple[str, Any]) -> tuple[float, float]:
        family_metrics = item[1]
        deviation = statistics.mean(
            abs(float(family_metrics["coverage"][str(level)]["empirical_coverage"]) - level)
            for level in (0.8, 0.9)
        )
        return deviation, float(family_metrics["brier_score"])

    selected_model, selected = min(metrics.items(), key=ranking)
    for level in (0.8, 0.9):
        empirical = float(selected["coverage"][str(level)]["empirical_coverage"])
        if abs(empirical - level) > 0.1:
            reasons.append(f"{level:.0%} coverage is outside the +/-10 percentage-point gate")
    if int(selected["false_safe_count"]) > 0:
        reasons.append("the selected model produced at least one false-safe prediction")
    return not reasons, selected_model, tuple(reasons)


def _persist_predictions(
    settings: Settings,
    campaign_id: uuid.UUID,
    candidate: Candidate,
    sequence_number: int,
    features: tuple[float, ...],
    forecasts: list[Forecast],
) -> list[Forecast]:
    candidate_key = _candidate_key(candidate.configuration)
    persisted: list[Forecast] = []
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        for forecast in forecasts:
            prediction_id = uuid.uuid4()
            cur.execute(
                """INSERT INTO charm_control.calibration_predictions
                (prediction_id,campaign_id,candidate_key,sequence_number,model_family,target_name,
                 nominal_coverage,feature_vector,point_prediction,lower_bound,upper_bound,
                 continuous_feasibility_probability,categorical_feasibility_probability,
                 direct_joint_feasibility_probability,joint_probabilities,selected_joint_rule,
                 selected_joint_probability,training_observation_ids,calibration_observation_ids)
                VALUES (%s,%s,%s,%s,%s,'p99_margin_ms',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    prediction_id,
                    campaign_id,
                    candidate_key,
                    sequence_number,
                    forecast.model_family,
                    forecast.nominal_coverage,
                    Jsonb(features),
                    forecast.point_prediction,
                    forecast.lower_bound,
                    forecast.upper_bound,
                    forecast.continuous_probability,
                    forecast.categorical_probability,
                    forecast.direct_joint_probability,
                    Jsonb(forecast.joint_probabilities),
                    forecast.selected_joint_rule,
                    forecast.selected_joint_probability,
                    Jsonb([str(item) for item in forecast.training_observation_ids]),
                    Jsonb([str(item) for item in forecast.calibration_observation_ids]),
                ),
            )
            persisted.append(
                replace(
                    forecast,
                    prediction_id=prediction_id,
                    candidate_key=candidate_key,
                    sequence_number=sequence_number,
                )
            )
        conn.commit()
    return persisted


def _label_predictions(
    settings: Settings, forecasts: list[Forecast], margin_ms: float, categorical_feasible: bool
) -> list[Forecast]:
    labeled = label_forecasts(forecasts, margin_ms, categorical_feasible)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        for forecast in labeled:
            if forecast.prediction_id is None:
                raise ValueError("cannot label a forecast that was not persisted")
            cur.execute(
                """UPDATE charm_control.calibration_predictions
                SET observed_value=%s,observed_continuous_feasible=%s,
                    observed_categorical_feasible=%s,observed_joint_feasible=%s,
                    labeled_at=clock_timestamp()
                WHERE prediction_id=%s AND labeled_at IS NULL""",
                (
                    margin_ms,
                    margin_ms >= 0,
                    categorical_feasible,
                    margin_ms >= 0 and categorical_feasible,
                    forecast.prediction_id,
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError("prediction label update was not unique or was repeated")
        conn.commit()
    return labeled


def _historical_examples(settings: Settings, baseline_tps: float) -> list[CalibrationExample]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT f3.fidelity_observation_id, f2.configuration,
                      f2.throughput_tps AS f2_tps, f2.p99_ms AS f2_p99,
                      f2.failures AS f2_failures, f3.p99_ms AS f3_p99,
                      f3.failures AS f3_failures
               FROM charm_control.fidelity_observations f2
               JOIN charm_control.fidelity_observations f3
                 ON f3.campaign_id=f2.campaign_id AND f3.candidate_key=f2.candidate_key
                AND f3.fidelity=3 AND f3.replicate=0
               WHERE f2.fidelity=2 AND f2.replicate=0
                 AND f2.throughput_tps IS NOT NULL AND f2.p99_ms IS NOT NULL
                 AND f3.p99_ms IS NOT NULL
               ORDER BY f3.created_at"""
        )
        rows = cur.fetchall()
    examples: list[CalibrationExample] = []
    for row in rows:
        f2 = WorkloadResult(
            transactions=0,
            failures=int(row["f2_failures"]),
            throughput_tps=float(row["f2_tps"]),
            p50_ms=0,
            p95_ms=0,
            p99_ms=float(row["f2_p99"]),
            duration_seconds=0,
            latency_samples=0,
        )
        examples.append(
            CalibrationExample(
                row["fidelity_observation_id"],
                pair_features(dict(row["configuration"]), f2, baseline_tps),
                P99_SLO_MS - float(row["f3_p99"]),
                int(row["f3_failures"]) == 0,
            )
        )
    return examples


def run_calibration_pilot(
    settings: Settings,
    candidates: int = 16,
    f2_seconds: int = 5,
    f3_seconds: int = 30,
    seed: int = 20260716,
) -> CalibrationPilotResult:
    if candidates < 12 or f2_seconds < 3 or f3_seconds < 10:
        raise ValueError("calibration pilot budget is below its evidence floor")
    baseline_tps = _baseline_tps(settings)
    history = _historical_examples(settings, baseline_tps)
    campaign_id = uuid.uuid4()
    started = time.monotonic()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.campaigns
            (campaign_id,name,mode,status,objective_definition,constraint_definition)
            VALUES (%s,%s,'CALIBRATION_RISK','RUNNING',%s,%s)""",
            (
                campaign_id,
                f"calibration-risk-seed-{seed}",
                Jsonb({"maximize": "throughput_tps"}),
                Jsonb({"p99_ms_max": P99_SLO_MS, "failures_max": 0}),
            ),
        )
        conn.commit()

    all_labeled: list[Forecast] = []
    paired = 0
    status = "FAILED"
    try:
        for sequence, candidate in enumerate(sobol_candidates(candidates, seed)):
            f0_started = time.monotonic()
            metadata = discover_knobs(settings, set(candidate.configuration))
            validate_candidate(candidate.configuration, metadata)
            _persist_fidelity(
                settings,
                campaign_id,
                candidate,
                0,
                0,
                time.monotonic() - f0_started,
                "static-validation",
            )
            application = apply_configuration(settings, candidate.configuration)
            try:
                f2_settings = settings.model_copy(
                    update={
                        "benchmark_warmup_seconds": 2,
                        "benchmark_duration_seconds": f2_seconds,
                        "benchmark_seed": seed + sequence,
                    }
                )
                f2_trial, _path, f2 = benchmark_default(
                    f2_settings, candidate.configuration, "calibration-f2", 2, "calibration-f2"
                )
                _persist_fidelity(
                    settings,
                    campaign_id,
                    candidate,
                    2,
                    0,
                    f2.duration_seconds,
                    "reduced-runtime-calibration",
                    f2,
                    f2_trial,
                    feasible=f2.failures == 0 and f2.p99_ms <= P99_SLO_MS,
                )
                features = pair_features(candidate.configuration, f2, baseline_tps)
                forecasts: list[Forecast] = []
                if len(history) >= MINIMUM_TRAINING_EXAMPLES:
                    forecasts = _persist_predictions(
                        settings,
                        campaign_id,
                        candidate,
                        sequence,
                        features,
                        build_forecasts(history, features),
                    )
                f3_settings = settings.model_copy(
                    update={
                        "benchmark_warmup_seconds": 5,
                        "benchmark_duration_seconds": f3_seconds,
                        "benchmark_seed": seed + sequence,
                    }
                )
                f3_trial, _path, f3 = benchmark_default(
                    f3_settings, candidate.configuration, "calibration-f3", 3, "calibration-f3"
                )
                f3_observation_id = _persist_fidelity(
                    settings,
                    campaign_id,
                    candidate,
                    3,
                    0,
                    f3.duration_seconds,
                    "full-runtime-calibration-label",
                    f3,
                    f3_trial,
                    feasible=f3.failures == 0 and f3.p99_ms <= P99_SLO_MS,
                )
                margin = P99_SLO_MS - f3.p99_ms
                if forecasts:
                    all_labeled.extend(
                        _label_predictions(settings, forecasts, margin, f3.failures == 0)
                    )
                history.append(
                    CalibrationExample(f3_observation_id, features, margin, f3.failures == 0)
                )
                paired += 1
            finally:
                rollback_configuration(
                    settings,
                    application.application_id,
                    f"restore after calibration pair {sequence}",
                )
        status = "COMPLETED"
    finally:
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.campaigns SET status=%s,updated_at=clock_timestamp()
                   WHERE campaign_id=%s""",
                (status, campaign_id),
            )
            conn.commit()

    scored_keys = {item.candidate_key for item in all_labeled if item.candidate_key is not None}
    representative = [
        item for item in all_labeled if item.nominal_coverage == 0.5 and item.model_family == "gp"
    ]
    feasible_labels = sum(bool(item.observed_joint_feasible) for item in representative)
    infeasible_labels = len(representative) - feasible_labels
    metrics = score_forecasts(all_labeled)
    enabled, selected_model, reasons = activation_gate(metrics, feasible_labels, infeasible_labels)
    gate = {
        "enabled": enabled,
        "selected_model": selected_model,
        "reasons": reasons,
        "minimum_scored_candidates": 12,
        "minimum_labels_per_class": 3,
        "coverage_tolerance": 0.1,
        "false_safe_tolerance": 0,
        "note": "calibration-adjusted until this empirical gate passes",
    }
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.calibration_reports
            (report_id,campaign_id,scored_candidates,feasible_labels,infeasible_labels,
             metrics,activation_gate,probabilistic_promotion_enabled)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                uuid.uuid4(),
                campaign_id,
                len(scored_keys),
                feasible_labels,
                infeasible_labels,
                Jsonb(metrics),
                Jsonb(gate),
                enabled,
            ),
        )
        conn.commit()
    return CalibrationPilotResult(
        campaign_id,
        paired,
        len(scored_keys),
        feasible_labels,
        infeasible_labels,
        enabled,
        selected_model,
        reasons,
        time.monotonic() - started,
    )


def result_json(result: CalibrationPilotResult) -> str:
    return json.dumps(
        {
            "campaign_id": str(result.campaign_id),
            "paired_candidates": result.paired_candidates,
            "scored_candidates": result.scored_candidates,
            "feasible_labels": result.feasible_labels,
            "infeasible_labels": result.infeasible_labels,
            "probabilistic_promotion_enabled": result.probabilistic_promotion_enabled,
            "selected_model": result.selected_model,
            "activation_reasons": result.activation_reasons,
            "wall_clock_seconds": result.wall_clock_seconds,
        },
        indent=2,
    )
