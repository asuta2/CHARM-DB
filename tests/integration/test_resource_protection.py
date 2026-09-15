import os
import uuid
from pathlib import Path

import pytest

from charmdb.config import get_settings
from charmdb.db import apply_migrations
from charmdb.resources import ResourceLimitError, capture_and_validate_resources
from charmdb.worker import (
    control_campaign,
    create_baseline_benchmark_trial,
    create_campaign,
    run_once,
    trial_history,
)

pytestmark = pytest.mark.integration


def _configured() -> bool:
    return bool(os.getenv("CHARMDB_TARGET_DSN") or Path(".env").exists())


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_live_resource_snapshot_has_declared_headroom() -> None:
    snapshot = capture_and_validate_resources(get_settings())
    assert snapshot["artifact_disk"]["free_bytes"] > 0
    assert snapshot["target_disk"]["free_bytes"] > 0
    assert snapshot["container"]["memory_limit_bytes"] == 4 * 1024**3
    assert snapshot["container"]["oom_killed"] is False
    assert snapshot["container"]["health"] == "healthy"


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_low_disk_gate_persists_resource_limit_before_workload() -> None:
    settings = get_settings().model_copy(update={"min_host_free_bytes": 2**63 - 1})
    apply_migrations(settings.control_dsn)
    campaign_id = create_campaign(
        settings,
        f"resource-limit-{uuid.uuid4().hex[:8]}",
        "RELIABILITY_NEGATIVE",
        {},
        {"host_disk_headroom": True},
        actor="pytest",
    )
    control_campaign(settings, campaign_id, "resume", "start disk gate negative case", "pytest")
    trial_id = create_baseline_benchmark_trial(
        settings,
        campaign_id,
        20260727,
        f"resource-limit-{uuid.uuid4()}",
        warmup_seconds=0,
        duration_seconds=1,
        concurrency=1,
        p99_slo_ms=1000.0,
        max_attempts=1,
    )
    with pytest.raises(ResourceLimitError, match="artifact disk"):
        run_once(
            settings,
            owner="resource-negative-worker",
            lease_seconds=30,
            campaign_id=campaign_id,
        )
    history = trial_history(settings, trial_id)
    assert history["state"] == "RESOURCE_LIMIT_EXCEEDED"
    assert history["failure_type"] == "RESOURCE_LIMIT_EXCEEDED"
    assert all(action["state"] != "RUNNING_FULL_EVALUATION" for action in history["actions"])
    control_campaign(settings, campaign_id, "stop", "resource gate negative retained", "pytest")
