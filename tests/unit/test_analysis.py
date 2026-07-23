from charmdb.analysis import _registry_complete


def test_registry_completion_requires_every_group_and_arm() -> None:
    assert _registry_complete(19, 19, 168, 168, 0)
    assert not _registry_complete(19, 18, 168, 168, 0)
    assert not _registry_complete(19, 19, 168, 167, 1)
