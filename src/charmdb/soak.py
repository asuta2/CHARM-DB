from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass

from charmdb.config import Settings
from charmdb.worker import (
    control_campaign,
    create_campaign,
    create_health_trial,
    run_once,
)

_DURATION = re.compile(r"^(?P<value>[1-9][0-9]*)(?P<unit>s|m|h)$")


@dataclass(frozen=True)
class SoakResult:
    campaign_id: uuid.UUID
    requested_seconds: int
    elapsed_seconds: float
    completed_trials: int
    failed_trials: int


def parse_duration(value: str) -> int:
    match = _DURATION.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError("duration must be a positive integer followed by s, m, or h")
    multiplier = {"s": 1, "m": 60, "h": 3600}[match.group("unit")]
    return int(match.group("value")) * multiplier


def run_soak_test(
    settings: Settings,
    duration: str,
    interval_seconds: float = 5.0,
    lease_seconds: int = 30,
) -> SoakResult:
    requested = parse_duration(duration)
    if interval_seconds <= 0:
        raise ValueError("soak interval must be positive")
    campaign_id = create_campaign(
        settings,
        f"soak-{duration}-{uuid.uuid4().hex[:8]}",
        "SOAK",
        {"monitor": "target_health"},
        {"health_required": True},
        failure_limit=max(5, requested // max(1, int(interval_seconds))),
        campaign_settings={
            "requested_duration_seconds": requested,
            "interval_seconds": interval_seconds,
        },
        actor="soak",
    )
    control_campaign(settings, campaign_id, "resume", "start timed soak", "soak")
    started = time.monotonic()
    completed = 0
    failed = 0
    sequence = 0
    try:
        while time.monotonic() - started < requested:
            create_health_trial(
                settings,
                campaign_id,
                20260713 + sequence,
                f"soak-health-{sequence}",
                max_attempts=3,
            )
            result = run_once(
                settings,
                owner=f"soak:{campaign_id}",
                lease_seconds=lease_seconds,
                campaign_id=campaign_id,
            )
            if result.state == "COMPLETED":
                completed += 1
            else:
                failed += 1
            sequence += 1
            remaining = requested - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(min(interval_seconds, remaining))
    finally:
        control_campaign(settings, campaign_id, "stop", "timed soak ended", "soak")
    return SoakResult(campaign_id, requested, time.monotonic() - started, completed, failed)


def soak_result_dict(result: SoakResult) -> dict[str, object]:
    payload = asdict(result)
    payload["campaign_id"] = str(result.campaign_id)
    return payload
