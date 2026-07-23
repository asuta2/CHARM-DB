from charmdb.experiments import (
    ABLATIONS,
    DEFAULT_SEEDS,
    REQUIRED_METRICS,
    deterministic_arm_order,
    experiment_group_specs,
)


def test_registry_contains_seven_groups_and_twelve_ablation_pairs() -> None:
    specs = experiment_group_specs()
    comparisons = [item for item in specs if item.kind == "COMPARISON"]
    ablations = [item for item in specs if item.kind == "ABLATION"]

    assert len(comparisons) == 7
    assert len(ablations) == 12
    assert {item.key.removeprefix("ablation_") for item in ablations} == set(ABLATIONS)
    assert all(item.arms[0] == "full_pipeline" and len(item.arms) == 2 for item in ablations)
    assert "effect_size" in REQUIRED_METRICS


def test_arm_order_is_seeded_and_preserves_equal_arm_set() -> None:
    arms = experiment_group_specs()[0].arms
    first = deterministic_arm_order(arms, DEFAULT_SEEDS[0])
    repeated = deterministic_arm_order(arms, DEFAULT_SEEDS[0])
    another = deterministic_arm_order(arms, DEFAULT_SEEDS[1])

    assert first == repeated
    assert set(first) == set(arms)
    assert first != another
