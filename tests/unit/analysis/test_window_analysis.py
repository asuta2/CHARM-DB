from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from charmdb.analysis.windows import (
    analyze_measurement_marker,
    analyze_prefix_windows,
    compare_duration_windows,
)


def test_measurement_marker_produces_prefix_second_and_rolling_windows(
    tmp_path: Path,
) -> None:
    started = datetime(2026, 8, 1, tzinfo=UTC)
    prefix = tmp_path / "pgbench-window"
    lines = []
    for index in range(20):
        timestamp = started.timestamp() + 15 + index * 30
        seconds = int(timestamp)
        micros = round((timestamp - seconds) * 1_000_000)
        lines.append(f"0 {index + 1} {1000 + index} 0 {seconds} {micros}")
    (tmp_path / "pgbench-window.1").write_text("\n".join(lines) + "\n", encoding="utf-8")
    marker = tmp_path / "measurement-attempt-1.json"
    marker.write_text(
        json.dumps(
            {
                "duration_seconds": 600,
                "started_at": started.isoformat(),
                "completed_at": datetime.fromtimestamp(
                    started.timestamp() + 600, UTC
                ).isoformat(),
                "command": ["pgbench", f"--log-prefix={prefix}"],
                "result": {
                    "duration_seconds": 600,
                    "throughput_tps": 1 / 30,
                    "p99_ms": 1.019,
                },
            }
        ),
        encoding="utf-8",
    )

    result = analyze_measurement_marker(marker)

    assert result["first_window"]["transactions"] == 10
    assert result["second_window"]["transactions"] == 10
    assert result["full_window"]["transactions"] == 20
    assert len(result["rolling_windows"]) == 10
    prefixes = analyze_prefix_windows(marker, (60, 120))
    assert prefixes[60].transactions == 2
    assert prefixes[120].transactions == 4


def test_duration_comparison_applies_rank_pareto_and_late_drift_gates() -> None:
    rows = []
    for index, (tps, p99) in enumerate(((3000.0, 40.0), (2600.0, 32.0), (2200.0, 25.0))):
        rows.append(
            {
                "configuration_name": f"c{index}",
                "first_window": {"throughput_tps": tps, "p99_ms": p99},
                "second_window": {"throughput_tps": tps * 1.001, "p99_ms": p99 + 0.1},
                "full_window": {"throughput_tps": tps * 1.0005, "p99_ms": p99 + 0.05},
            }
        )

    comparison = compare_duration_windows(rows)

    assert comparison["passed"] is True
    assert comparison["tps_rank_correlation"] == 1.0
    assert comparison["p99_rank_correlation"] == 1.0
    assert comparison["pareto_membership_agreement"] == 1.0


def test_duration_comparison_aggregates_repeated_blocks_by_configuration() -> None:
    rows = []
    for block in range(3):
        for name, tps, p99 in (
            ("fast", 3000.0, 40.0),
            ("balanced", 2600.0, 30.0),
            ("low-latency", 2200.0, 20.0),
        ):
            rows.append(
                {
                    "configuration_name": name,
                    "first_window": {
                        "throughput_tps": tps + block,
                        "p99_ms": p99 + block * 0.01,
                    },
                    "second_window": {
                        "throughput_tps": tps + block + 1,
                        "p99_ms": p99 + block * 0.01 + 0.1,
                    },
                    "full_window": {
                        "throughput_tps": tps + block + 0.5,
                        "p99_ms": p99 + block * 0.01 + 0.05,
                    },
                }
            )

    comparison = compare_duration_windows(rows)

    assert comparison["paired_rows"] == 9
    assert comparison["configurations"] == 3
    assert comparison["first_window_pareto"] == ["balanced", "fast", "low-latency"]
    assert comparison["passed"] is True
