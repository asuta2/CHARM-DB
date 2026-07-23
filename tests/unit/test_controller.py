import pytest

from charmdb.controller import validate_candidate

METADATA = [
    {
        "name": "random_page_cost",
        "context": "user",
        "vartype": "real",
        "unit": None,
        "min_val": "0",
        "max_val": "1.79769e+308",
        "enumvals": None,
    }
]


def test_validate_candidate_accepts_runtime_bound() -> None:
    validate_candidate({"random_page_cost": "3.9"}, METADATA)


def test_validate_candidate_rejects_out_of_bounds() -> None:
    with pytest.raises(ValueError, match="above research maximum"):
        validate_candidate({"random_page_cost": "11"}, METADATA)


def test_validate_candidate_rejects_unbounded_knob() -> None:
    with pytest.raises(ValueError, match="bounded runtime allowlist"):
        validate_candidate({"fsync": "off"}, METADATA)
