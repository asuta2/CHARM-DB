import os

import pytest

from charmdb.config import get_settings
from charmdb.resources import capture_and_validate_resources

pytestmark = pytest.mark.integration


def _configured() -> bool:
    return bool(os.getenv("CHARMDB_DISPOSABLE_INTEGRATION") == "1")


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_live_resource_snapshot_has_declared_headroom() -> None:
    snapshot = capture_and_validate_resources(get_settings())
    assert snapshot["artifact_disk"]["free_bytes"] > 0
    assert snapshot["target_disk"]["free_bytes"] > 0
    assert snapshot["container"]["memory_limit_bytes"] == 4 * 1024**3
    assert snapshot["container"]["oom_killed"] is False
    assert snapshot["container"]["health"] == "healthy"
