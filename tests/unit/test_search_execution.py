import json
import uuid
from types import SimpleNamespace

import pytest

import charmdb.search_execution as search
from charmdb.optimizer import Candidate, Observation


def observations(count: int) -> list[Observation]:
    return [
        Observation(
            uuid.uuid4(),
            (index / 10, 0.5, 0.5),
            {
                "random_page_cost": f"{1 + index / 10:g}",
                "work_mem": "4096",
                "effective_io_concurrency": "100",
            },
            500.0 + index,
            12.0,
            0,
            True,
        )
        for index in range(count)
    ]


def test_precomputed_search_schedules_are_deterministic_and_distinct() -> None:
    arm_id = uuid.uuid4()
    key = search.search_trial_idempotency_key(arm_id, 991, "random", 3)
    random_first = search.recommend_search_position("random", 991, 3, [])
    random_repeated = search.recommend_search_position("random", 991, 3, [])
    sobol = search.recommend_search_position("sobol", 991, 3, [])
    default = search.recommend_search_position("postgresql_default", 991, 3, [])

    assert key == f"experiment:{arm_id}:991:random:3"
    assert random_first == random_repeated
    assert random_first.candidate != sobol.candidate
    assert default.candidate is None
    assert default.stage == "DEFAULT"


def test_bo_waits_for_persisted_observations_and_dispatches_constraint_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = search.recommend_search_position("constrained_bo", 991, 2, observations(2))
    calls: list[tuple[bool, set[str]]] = []

    def fake_recommend(*_args: object, **kwargs: object) -> tuple[Candidate, float, float]:
        calls.append((bool(kwargs["constrained"]), _args[2]))  # type: ignore[arg-type]
        return (
            Candidate(
                (0.1, 0.2, 0.3),
                {"random_page_cost": "1.3", "work_mem": "2048", "effective_io_concurrency": "60"},
            ),
            1.5,
            0.9,
        )

    monkeypatch.setattr(search, "recommend_multiobjective", fake_recommend)
    failed_configuration = {
        "random_page_cost": "3.9",
        "work_mem": "32768",
        "effective_io_concurrency": "200",
    }
    excluded = {json.dumps(failed_configuration, sort_keys=True)}
    standard = search.recommend_search_position(
        "standard_bo", 991, 4, observations(4), excluded_configurations=excluded
    )
    constrained = search.recommend_search_position(
        "constrained_bo", 991, 4, observations(4), excluded_configurations=excluded
    )

    assert initial.stage == "BO_INITIALIZATION"
    assert [item[0] for item in calls] == [False, True]
    assert all(excluded <= item[1] for item in calls)
    assert standard.stage == "STANDARD_BO"
    assert constrained.stage == "CONSTRAINED_BO"
    assert len(constrained.training_observation_ids) == 4


def test_bo_initialization_count_is_explicit_and_persisted_row_round_trips() -> None:
    still_initializing = search.recommend_search_position(
        "constrained_bo", 991, 4, observations(4), initial_observations=5
    )
    assert still_initializing.stage == "BO_INITIALIZATION"

    candidate = search._sobol_candidate_at_position(4, 991)
    observation_id = uuid.uuid4()
    persisted_row = {
        "method": "constrained_bo",
        "stage": "CONSTRAINED_BO",
        "budget_position": 4,
        "random_seed": 995,
        "input_vector": list(candidate.vector),
        "configuration": candidate.configuration,
        "training_observation_ids": [str(observation_id)],
        "acquisition_name": "qLogNEHVI_CONSTRAINED",
        "acquisition_value": 1.25,
        "probability_feasible": 0.75,
    }
    reconstructed = search._recommendation_from_row(persisted_row)
    assert reconstructed.candidate == candidate
    assert reconstructed.training_observation_ids == (observation_id,)

    with pytest.raises(ValueError, match="vector and configuration"):
        search._recommendation_from_row(
            {
                **persisted_row,
                "configuration": {**candidate.configuration, "random_page_cost": "4"},
            }
        )
    with pytest.raises(ValueError, match=r"outside \[0, 1\]"):
        search._recommendation_from_row({**persisted_row, "probability_feasible": 1.1})


def test_whole_arm_driver_reuses_durable_start_until_finalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arm_id = uuid.uuid4()
    campaign_id = uuid.uuid4()
    trial_id = uuid.uuid4()
    running = search.SearchArmProgress(
        arm_id, campaign_id, "random", "RUNNING", 1.0, 2, trial_id, "RESUMED_ACTIVE_TRIAL"
    )
    finalized = search.SearchArmProgress(
        arm_id, campaign_id, "random", "COMPLETED", 20.0, 21, None, "FINALIZED"
    )
    progress = iter([running, finalized])
    worker_campaigns: list[uuid.UUID] = []

    monkeypatch.setattr(search, "start_or_resume_search_arm", lambda *_args: next(progress))

    def fake_run_once(*_args: object, **kwargs: object) -> SimpleNamespace:
        worker_campaigns.append(kwargs["campaign_id"])  # type: ignore[arg-type]
        return SimpleNamespace(claimed=True)

    monkeypatch.setattr(search, "run_once", fake_run_once)

    result = search.run_search_arm(object(), arm_id)  # type: ignore[arg-type]

    assert result == finalized
    assert worker_campaigns == [campaign_id]
