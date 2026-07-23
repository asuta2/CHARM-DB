import json
import uuid

import torch

from charmdb.optimizer import (
    Observation,
    decode_vector,
    encode_configuration,
    recommend_constrained_bo,
    sobol_candidates,
)


def test_encode_decode_uses_bounded_native_units() -> None:
    candidate = decode_vector(torch.tensor([0.5, 0.4, 0.25], dtype=torch.double))
    assert candidate.configuration == {
        "random_page_cost": "2.5",
        "work_mem": "4096",
        "effective_io_concurrency": "50",
    }
    encoded = encode_configuration(candidate.configuration)
    assert encoded == (0.5, 0.4, 0.25)


def test_sobol_candidates_are_deterministic_and_unique() -> None:
    first = sobol_candidates(8, 42)
    second = sobol_candidates(8, 42)
    assert first == second
    assert len({tuple(item.configuration.items()) for item in first}) == 8


def test_constrained_bo_returns_nonduplicate_with_probability() -> None:
    candidates = sobol_candidates(4, 7)
    observations = [
        Observation(
            uuid.uuid4(),
            candidate.vector,
            candidate.configuration,
            450.0 + index * 10,
            12.0 + index,
            0,
            True,
        )
        for index, candidate in enumerate(candidates)
    ]
    excluded = {json.dumps(candidate.configuration, sort_keys=True) for candidate in candidates}
    candidate, value, probability = recommend_constrained_bo(
        observations, 8, excluded, pool_size=64, sample_count=32
    )
    assert json.dumps(candidate.configuration, sort_keys=True) not in excluded
    assert value == value
    assert 0 <= probability <= 1
