from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from charmdb.analysis.windows import analyze_rolling_measurement
from charmdb.campaigns.warmup_duration import (
    _measurement_marker,
    duration_schedule,
    pilot_manifest_payload,
)
from charmdb.config import Settings

ROOT = Path(__file__).resolve().parents[3]


def test_pilot_manifest_preserves_operator_and_subphase_gates() -> None:
    digest, payload = pilot_manifest_payload(
        ROOT / "experiments/thesis/manifests/warmup-and-f3-duration-pilot.json"
    )

    assert len(digest) == 64
    assert payload["status"] == "ready"
    assert payload["restore_inclusive_runtime_accepted"] is True
    assert payload["unresolved_decisions"] == []
    assert payload["future_decisions"] == [
        "freeze warmup duration",
        "select 300 or 600 second F3 duration",
    ]
    assert len(payload["warmup_calibration"]["representative_configurations"]) == 4
    assert len(payload["duration_pilot"]["representative_configurations"]) == 6
    assert payload["duration_pilot"]["schedule_seed"] == 20261017


def test_duration_schedule_is_deterministic_balanced_and_block_randomized() -> None:
    names = [f"configuration-{index}" for index in range(6)]
    first = duration_schedule(
        names,
        blocks=3,
        standalone_configuration=names[0],
        schedule_seed=20261017,
    )
    second = duration_schedule(
        names,
        blocks=3,
        standalone_configuration=names[0],
        schedule_seed=20261017,
    )

    assert first == second
    assert len(first) == 21
    for block in range(1, 4):
        rows = [row for row in first if row["block"] == block]
        assert len(rows) == 7
        assert {row["configuration_name"] for row in rows} == set(names)
        assert sum(row["subphase"] == "DURATION_SHORT" for row in rows) == 1
        assert rows != sorted(rows, key=lambda row: (row["subphase"], row["configuration_name"]))


def test_rolling_warmup_analysis_uses_entire_measurement(tmp_path: Path) -> None:
    started = datetime(2026, 8, 2, tzinfo=UTC)
    prefix = tmp_path / "pgbench-warmup"
    lines = []
    for index in range(20):
        timestamp = started.timestamp() + 30 + index * 60
        seconds = int(timestamp)
        micros = round((timestamp - seconds) * 1_000_000)
        lines.append(f"0 {index + 1} {1000 + index} 0 {seconds} {micros}")
    (tmp_path / "pgbench-warmup.1").write_text("\n".join(lines) + "\n", encoding="utf-8")
    marker = tmp_path / "measurement-attempt-1.json"
    marker.write_text(
        json.dumps(
            {
                "duration_seconds": 1200,
                "started_at": started.isoformat(),
                "command": ["pgbench", f"--log-prefix={prefix}"],
                "result": {"duration_seconds": 1200, "throughput_tps": 1 / 60},
                "runtime_telemetry": {"runtime_samples": [{"elapsed_seconds": 5}]},
            }
        ),
        encoding="utf-8",
    )

    result = analyze_rolling_measurement(marker)

    assert len(result["rolling_windows"]) == 20
    assert result["rolling_windows"][0]["transactions"] == 1
    assert result["rolling_windows"][-1]["end_seconds"] == 1200
    assert len(result["runtime_telemetry"]["runtime_samples"]) == 1


def test_measurement_marker_falls_back_to_durable_trial_artifact(tmp_path: Path) -> None:
    campaign_id = uuid.uuid4()
    trial_id = uuid.uuid4()
    output_dir = tmp_path / "raw" / str(campaign_id) / str(trial_id)
    output_dir.mkdir(parents=True)
    marker = output_dir / "measurement-attempt-1.json"
    marker.write_text("{}", encoding="utf-8")
    relative = marker.relative_to(tmp_path)
    (output_dir / "trial.json").write_text(
        json.dumps({"measurement_marker": str(relative)}),
        encoding="utf-8",
    )
    settings = Settings.model_construct(artifact_dir=tmp_path)

    resolved = _measurement_marker(
        settings,
        {
            "run_id": uuid.uuid4(),
            "campaign_id": campaign_id,
            "trial_id": trial_id,
            "workflow_result": {},
        },
    )

    assert resolved == marker.resolve()
