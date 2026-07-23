import pytest

from charmdb.soak import parse_duration


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("1s", 1), ("5m", 300), ("1h", 3600), ("24h", 86400)],
)
def test_parse_soak_duration(value: str, seconds: int) -> None:
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["", "0s", "1", "1d", "-1h", "1.5h"])
def test_reject_invalid_soak_duration(value: str) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration(value)
