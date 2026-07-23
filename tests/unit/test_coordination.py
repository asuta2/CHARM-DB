import uuid

from charmdb.coordination import IndexOption, KnobOption, select_action_schedule


def _options() -> tuple[list[KnobOption], list[IndexOption]]:
    knobs = [
        KnobOption({"random_page_cost": "2"}, 3.0),
        KnobOption({"random_page_cost": "3"}, 2.0),
        KnobOption({"random_page_cost": "4"}, 1.0),
    ]
    indexes = [IndexOption((uuid.UUID(int=index),), 3.0 - index) for index in range(1, 4)]
    return knobs, indexes


def test_all_action_strategies_preserve_equal_budget() -> None:
    knobs, indexes = _options()
    for strategy in (
        "knob_only",
        "index_only",
        "knobs_then_indexes",
        "indexes_then_knobs",
        "alternating",
        "coordinated",
    ):
        actions = select_action_schedule(strategy, knobs, indexes, budget=5)
        assert len(actions) == 5


def test_sequential_order_and_alternation_are_explicit() -> None:
    knobs, indexes = _options()
    knobs_first = select_action_schedule("knobs_then_indexes", knobs, indexes, budget=4)
    indexes_first = select_action_schedule("indexes_then_knobs", knobs, indexes, budget=4)
    alternating = select_action_schedule("alternating", knobs, indexes, budget=4)
    assert [bool(item.knob_configuration) for item in knobs_first] == [True, True, False, False]
    assert [bool(item.knob_configuration) for item in indexes_first] == [False, False, True, True]
    assert [bool(item.knob_configuration) for item in alternating] == [True, False, True, False]


def test_coordinated_selector_can_rank_an_interaction_not_seen_marginally() -> None:
    knobs, indexes = _options()
    interaction = {('{"random_page_cost": "4"}', indexes[2].candidate_ids): 10.0}
    selected = select_action_schedule(
        "coordinated", knobs, indexes, budget=1, interaction_scores=interaction
    )
    assert selected[0].knob_configuration == {"random_page_cost": "4"}
    assert selected[0].index_candidate_ids == indexes[2].candidate_ids
