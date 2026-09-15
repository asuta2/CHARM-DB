from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from typing import Any

import pytest
import torch

from charmdb.optimization.bo import (
    PrimaryObservation,
    primary_acquisition_name,
    primary_hypervolume,
    recommend_primary_bo,
)
from charmdb.optimization.design import PRIMARY_MANIFEST, primary_method_design
from charmdb.protocol import load_manifest

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / PRIMARY_MANIFEST


class _FakeAcquisition:
    def __call__(self, candidates: torch.Tensor) -> torch.Tensor:
        return candidates.sum(dim=(-1, -2))


def _observations() -> tuple[dict[str, Any], list[PrimaryObservation]]:
    payload = load_manifest(MANIFEST).payload
    candidates = primary_method_design(88408573, "bo_shared_initial", payload)
    observations = [
        PrimaryObservation(
            uuid.uuid4(),
            candidate.vector,
            candidate.configuration,
            2500.0 + index * 10,
            35.0 - index * 0.2,
            0,
            True,
        )
        for index, candidate in enumerate(candidates[:6])
    ]
    return payload, observations


@pytest.mark.parametrize(
    ("method", "constructor", "expected_name", "expected_outputs"),
    [
        ("bo_qlognei_throughput", "qLogNoisyExpectedImprovement", "qLogNEI", 1),
        ("bo_qlognparego_multiobjective", "qLogNParEGO", "qLogNParEGO", 2),
        (
            "bo_qlognehvi_multiobjective",
            "qLogNoisyExpectedHypervolumeImprovement",
            "qLogNEHVI",
            2,
        ),
    ],
)
def test_primary_bo_dispatch_has_no_constraint_model_or_constraint_argument(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    constructor: str,
    expected_name: str,
    expected_outputs: int,
) -> None:
    payload, observations = _observations()
    captured: dict[str, Any] = {}

    def fake_constructor(**kwargs: Any) -> _FakeAcquisition:
        captured.update(kwargs)
        return _FakeAcquisition()

    monkeypatch.setattr("charmdb.optimization.bo.fit_gpytorch_mll", lambda model: model)
    monkeypatch.setattr(f"charmdb.optimization.bo.{constructor}", fake_constructor)
    excluded = {
        json.dumps(item.configuration, sort_keys=True, separators=(",", ":"))
        for item in observations
    }
    recommendation = recommend_primary_bo(  # type: ignore[arg-type]
        method,
        observations,
        seed=97,
        excluded=excluded,
        payload=payload,
        pool_size=16,
        sample_count=16,
    )

    assert recommendation.acquisition_name == expected_name
    assert captured["constraints"] is None
    assert captured["model"].num_outputs == expected_outputs
    assert len(recommendation.candidate.vector) == 8
    assert recommendation.training_run_ids == tuple(item.run_id for item in observations)


def test_primary_registry_rejects_default_as_a_method() -> None:
    assert primary_acquisition_name("random") is None
    assert primary_acquisition_name("sobol") is None
    with pytest.raises(ValueError, match="unsupported primary search method"):
        primary_acquisition_name("postgresql_default")


def test_primary_hypervolume_uses_throughput_and_negative_p99_at_frozen_reference() -> None:
    first = PrimaryObservation(uuid.uuid4(), (0.1,) * 8, {}, 100.0, 20.0, 0, True)
    second = PrimaryObservation(uuid.uuid4(), (0.2,) * 8, {}, 120.0, 30.0, 0, True)
    invalid = PrimaryObservation(uuid.uuid4(), (0.3,) * 8, {}, 500.0, 10.0, 1, False)

    assert primary_hypervolume([first, second, invalid]) == pytest.approx(2200.0)


BO_METHOD_NAMES = [
    ("bo_qlognei_throughput", "qLogNEI"),
    ("bo_qlognparego_multiobjective", "qLogNParEGO"),
    ("bo_qlognehvi_multiobjective", "qLogNEHVI"),
]


def _within_manifest_bounds(configuration: dict[str, str], payload: dict[str, Any]) -> None:
    """Assert every decoded value sits inside its frozen range."""
    parameters = dict(payload["search_space"]["parameters"])
    assert set(configuration) == set(payload["search_space"]["parameter_order"])
    for name, rendered in configuration.items():
        specification = dict(parameters[name])
        value = float(rendered)
        assert float(specification["lower"]) <= value <= float(specification["upper"]), (
            f"{name}={rendered} escaped [{specification['lower']},{specification['upper']}]"
        )


@pytest.mark.parametrize(("method", "expected_name"), BO_METHOD_NAMES)
def test_live_bayesian_path_proposes_in_bounds_at_production_settings(
    method: str, expected_name: str
) -> None:
    """Run the real BoTorch path unmocked at the production pool and sample size.

    The dispatch test above stubs out `fit_gpytorch_mll` and the acquisition
    constructor, so it cannot catch a genuine fit, acquisition-construction, or
    pool-evaluation failure. This exercises all three for real, at the
    `pool_size=2048` / `sample_count=256` defaults the runner actually uses,
    because that is the configuration a Wave A adaptive slot depends on.
    """
    payload, observations = _observations()

    recommendation = recommend_primary_bo(  # type: ignore[arg-type]
        method, observations, seed=4242, excluded=set(), payload=payload
    )

    assert recommendation.acquisition_name == expected_name
    assert math.isfinite(recommendation.acquisition_value)
    assert len(recommendation.candidate.vector) == 8
    assert all(0.0 <= item <= 1.0 for item in recommendation.candidate.vector)
    assert recommendation.training_run_ids == tuple(item.run_id for item in observations)
    _within_manifest_bounds(recommendation.candidate.configuration, payload)


def test_live_bayesian_path_is_reproducible_for_a_fixed_seed() -> None:
    """A repeated seed must reproduce the proposal exactly.

    Wave A's reproducibility claim depends on this. The comparison is made
    within one process rather than against a stored golden vector, so a BoTorch
    or torch upgrade cannot turn a valid numerical change into a false failure.
    """
    payload, observations = _observations()

    first = recommend_primary_bo(  # type: ignore[arg-type]
        "bo_qlognehvi_multiobjective", observations, seed=7, excluded=set(), payload=payload
    )
    repeated = recommend_primary_bo(  # type: ignore[arg-type]
        "bo_qlognehvi_multiobjective", observations, seed=7, excluded=set(), payload=payload
    )

    assert repeated.candidate.vector == first.candidate.vector
    assert repeated.candidate.configuration == first.candidate.configuration
    assert repeated.acquisition_value == first.acquisition_value


def test_live_bayesian_path_threads_the_seed_into_the_proposal() -> None:
    """A different acquisition seed must move the proposal."""
    payload, observations = _observations()

    first = recommend_primary_bo(  # type: ignore[arg-type]
        "bo_qlognehvi_multiobjective",
        observations,
        seed=11,
        excluded=set(),
        payload=payload,
        pool_size=64,
        sample_count=16,
    )
    other = recommend_primary_bo(  # type: ignore[arg-type]
        "bo_qlognehvi_multiobjective",
        observations,
        seed=12,
        excluded=set(),
        payload=payload,
        pool_size=64,
        sample_count=16,
    )

    assert other.candidate.vector != first.candidate.vector


def test_live_bayesian_path_never_reproposes_an_excluded_configuration() -> None:
    """The exclusion set is what keeps a slot from duplicating a known point."""
    payload, observations = _observations()
    common = {
        "observations": observations,
        "payload": payload,
        "pool_size": 64,
        "sample_count": 16,
    }

    first = recommend_primary_bo(  # type: ignore[arg-type]
        "bo_qlognei_throughput", seed=19, excluded=set(), **common
    )
    key = json.dumps(
        first.candidate.configuration, sort_keys=True, separators=(",", ":")
    )
    second = recommend_primary_bo(  # type: ignore[arg-type]
        "bo_qlognei_throughput", seed=19, excluded={key}, **common
    )

    assert second.candidate.configuration != first.candidate.configuration
    _within_manifest_bounds(second.candidate.configuration, payload)
