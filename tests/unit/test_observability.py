from charmdb.observability import _labels


def test_prometheus_labels_are_sorted_and_escaped() -> None:
    assert _labels({"workflow": 'A"B', "state": "X\\Y"}) == ('{state="X\\\\Y",workflow="A\\"B"}')
