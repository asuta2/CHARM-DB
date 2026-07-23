from charmdb.metrics import numeric_difference, percentile


def test_percentile_interpolates() -> None:
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([10], 0.99) == 10


def test_percentile_rejects_empty() -> None:
    try:
        percentile([], 0.5)
    except ValueError as error:
        assert "at least one" in str(error)
    else:
        raise AssertionError("empty percentile input was accepted")


def test_numeric_difference_is_recursive() -> None:
    before = {"database": {"xact_commit": 10, "name": "db"}, "size": 100}
    after = {"database": {"xact_commit": 17, "name": "db"}, "size": 130}
    assert numeric_difference(before, after) == {
        "database": {"xact_commit": 7, "name": "db"},
        "size": 30,
    }
