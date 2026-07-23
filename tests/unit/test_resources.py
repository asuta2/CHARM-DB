from types import SimpleNamespace

import pytest

from charmdb import resources
from charmdb.resources import (
    ResourceLimitError,
    capture_and_validate_resources,
    validate_resource_snapshot,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        min_host_free_bytes=100,
        min_target_free_bytes=200,
        max_container_memory_fraction=0.9,
    )


def _snapshot() -> dict[str, object]:
    return {
        "artifact_disk": {"free_bytes": 1_000},
        "target_disk": {"free_bytes": 2_000},
        "docker_engine": {"memory_total_bytes": 4_000},
        "container": {
            "oom_killed": False,
            "status": "running",
            "health": "healthy",
            "memory_usage_bytes": 400,
            "memory_limit_bytes": 1_000,
        },
    }


def test_resource_snapshot_accepts_healthy_headroom() -> None:
    validate_resource_snapshot(_settings(), _snapshot())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (("artifact_disk", "free_bytes", 99), "artifact disk"),
        (("target_disk", "free_bytes", 199), "target disk"),
        (("container", "oom_killed", True), "OOMKilled"),
        (("container", "health", "unhealthy"), "not healthy"),
        (("container", "memory_usage_bytes", 900), "memory fraction"),
    ],
)
def test_resource_snapshot_rejects_hard_limit(
    mutation: tuple[str, str, object], message: str
) -> None:
    snapshot = _snapshot()
    section, field, value = mutation
    snapshot[section][field] = value  # type: ignore[index]
    with pytest.raises(ResourceLimitError, match=message):
        validate_resource_snapshot(_settings(), snapshot)  # type: ignore[arg-type]


def test_resource_capture_waits_for_transient_container_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starting = _snapshot()
    starting["container"]["health"] = "starting"  # type: ignore[index]
    healthy = _snapshot()
    snapshots = iter((starting, healthy))
    monkeypatch.setattr(resources, "capture_resource_snapshot", lambda _settings: next(snapshots))

    result = capture_and_validate_resources(  # type: ignore[arg-type]
        _settings(), health_settle_seconds=1.0, health_poll_seconds=0.0
    )

    assert result is healthy
