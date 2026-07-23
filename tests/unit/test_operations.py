from types import SimpleNamespace

import pytest

from charmdb.operations import drift_history, index_history


@pytest.mark.parametrize("limit", [0, 1001])
def test_history_limits_fail_before_database_access(limit: int) -> None:
    settings = SimpleNamespace(control_dsn="must-not-connect")
    with pytest.raises(ValueError, match="between 1 and 1000"):
        drift_history(settings, limit=limit)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="between 1 and 1000"):
        index_history(settings, limit=limit)  # type: ignore[arg-type]
