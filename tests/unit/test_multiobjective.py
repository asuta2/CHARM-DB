import json
import uuid

import pytest

from charmdb.multiobjective import (
    feasible_pareto,
    pareto_hypervolume,
    recommend_multiobjective,
)
from charmdb.optimizer import Observation, sobol_candidates


def _observation(index: int, throughput: float, p99_ms: float) -> Observation:
    candidate = sobol_candidates(4, 17)[index]
    return Observation(
        uuid.uuid4(),
        candidate.vector,
        candidate.configuration,
        throughput,
        p99_ms,
        0,
        p99_ms <= 20,
    )


def test_pareto_and_hypervolume_use_throughput_and_negative_p99() -> None:
    first = _observation(0, 100.0, 10.0)
    second = _observation(1, 120.0, 15.0)
    dominated = _observation(2, 90.0, 12.0)
    infeasible = _observation(3, 200.0, 25.0)

    pareto = feasible_pareto([first, second, dominated, infeasible])

    assert {item.observation_id for item in pareto} == {
        first.observation_id,
        second.observation_id,
    }
    assert pareto_hypervolume([first, second, dominated, infeasible]) == pytest.approx(1100.0)


@pytest.mark.parametrize("acquisition", ["qLogNEHVI", "qLogNParEGO"])
def test_multiobjective_acquisitions_return_nonduplicate(acquisition: str) -> None:
    candidates = sobol_candidates(4, 21)
    observations = [
        Observation(
            uuid.uuid4(),
            candidate.vector,
            candidate.configuration,
            100.0 + index * 10,
            8.0 + index,
            0,
            True,
        )
        for index, candidate in enumerate(candidates)
    ]
    excluded = {json.dumps(item.configuration, sort_keys=True) for item in candidates}

    candidate, value, probability = recommend_multiobjective(
        observations,
        seed=31,
        excluded=excluded,
        acquisition_name=acquisition,  # type: ignore[arg-type]
        pool_size=32,
        sample_count=16,
    )

    assert json.dumps(candidate.configuration, sort_keys=True) not in excluded
    assert value == value
    assert 0 <= probability <= 1
