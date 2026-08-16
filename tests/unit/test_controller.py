import pytest

from charmdb.controller import settings_equivalent, validate_candidate

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


def test_real_setting_equivalence_accepts_postgresql_canonicalization() -> None:
    assert settings_equivalent(
        {"random_page_cost": "3.700422"},
        {"random_page_cost": "3.70042"},
        METADATA,
    )


def test_real_setting_equivalence_rejects_meaningful_drift() -> None:
    assert not settings_equivalent(
        {"random_page_cost": "3.700422"},
        {"random_page_cost": "3.7003"},
        METADATA,
    )


def test_non_real_setting_equivalence_remains_exact() -> None:
    integer_metadata = [{**METADATA[0], "name": "work_mem", "vartype": "integer"}]
    assert not settings_equivalent(
        {"work_mem": "4096"},
        {"work_mem": "4096.0"},
        integer_metadata,
    )
