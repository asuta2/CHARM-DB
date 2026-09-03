import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from charmdb.workload import (
    PGBENCH_MAINTENANCE_POLICY,
    MeasurementExecution,
    WorkloadResult,
    _pgbench_maintenance_arguments,
    _resolve_client_threads,
    load_measurement_marker,
    parse_pgbench_log_records,
    parse_pgbench_logs,
    run_pgbench_promoted_measurement,
)


def test_pgbench_startup_maintenance_is_disabled_for_timed_workloads() -> None:
    assert PGBENCH_MAINTENANCE_POLICY == "canonical-baseline-only-no-vacuum"
    assert _pgbench_maintenance_arguments() == ["--no-vacuum"]


def test_client_thread_cap_is_explicit_and_bounded_by_concurrency() -> None:
    assert _resolve_client_threads(64, None) == 4
    assert _resolve_client_threads(4, 4) == 4
    with pytest.raises(ValueError, match="between one and concurrency"):
        _resolve_client_threads(4, 8)


def test_parse_pgbench_logs(tmp_path: Path) -> None:
    log = tmp_path / "pgbench_log.1"
    log.write_text("0 1 1500 0 1\n0 2 2500 0 2\n", encoding="utf-8")
    latencies, transactions, failures = parse_pgbench_logs([log])
    assert latencies == [1.5, 2.5]
    assert transactions == 2
    assert failures == 0


def test_structured_pgbench_logs_preserve_epoch_timestamps(tmp_path: Path) -> None:
    log = tmp_path / "pgbench_log.1"
    log.write_text(
        "0 1 1500 0 1785612873 687420\n0 2 -1 0 1785612874 250000\n",
        encoding="utf-8",
    )

    records = parse_pgbench_log_records([log])

    assert records[0].timestamp_seconds == pytest.approx(1785612873.68742)
    assert records[0].latency_ms == 1.5
    assert records[1].failed is True


def test_atomic_measurement_marker_can_reconstruct_result(tmp_path: Path) -> None:
    marker = tmp_path / "measurement-attempt-2.json"
    marker.write_text(
        """{
          "command": ["pgbench", "-T", "5"],
          "stdout": "tps = 123.4",
          "stderr": "",
          "runtime_telemetry": {"client_cpu_seconds": 2.5},
          "result": {
            "transactions": 617,
            "failures": 0,
            "throughput_tps": 123.4,
            "p50_ms": 3.0,
            "p95_ms": 7.0,
            "p99_ms": 9.0,
            "duration_seconds": 5.01,
            "latency_samples": 617
          }
        }""",
        encoding="utf-8",
    )
    execution = load_measurement_marker(marker)
    assert execution.result.transactions == 617
    assert execution.result.p99_ms == 9.0
    assert execution.marker_path == marker
    assert execution.runtime_telemetry["client_cpu_seconds"] == 2.5


def test_promoted_measurement_aggregates_stage_logs_and_persists_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def fake_measurement(*args: object, **kwargs: object) -> MeasurementExecution:
        output_dir = Path(str(args[1]))
        duration = int(args[2])
        attempt = int(args[5])
        calls.append(duration)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"pgbench-stage-{duration}").write_text(
            "0 1 1000 0 1 0\n0 2 2000 0 2 0\n", encoding="utf-8"
        )
        started = datetime.now(UTC)
        marker = output_dir / f"measurement-attempt-{attempt}.json"
        result = WorkloadResult(2, 0, 100.0, 1.0, 2.0, 2.0, float(duration), 2)
        marker.write_text(
            json.dumps(
                {
                    "command": ["pgbench", "-T", str(duration)],
                    "stdout": "tps = 100.0",
                    "stderr": "",
                    "runtime_telemetry": {},
                    "result": asdict(result),
                    "started_at": started.isoformat(),
                    "completed_at": (started + timedelta(seconds=duration)).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        return MeasurementExecution(result, ["pgbench"], "", "", marker, {})

    monkeypatch.setattr("charmdb.workload.run_pgbench_measurement", fake_measurement)
    execution = run_pgbench_promoted_measurement(
        SimpleNamespace(),  # type: ignore[arg-type]
        tmp_path,
        60,
        540,
        32,
        1,
        2,
        1,
        100.0,
        0.8,
    )

    payload = json.loads(execution.marker_path.read_text(encoding="utf-8"))
    assert calls == [60, 540]
    assert payload["promotion"]["promoted"] is True
    assert payload["promotion"]["continuation_marker"] is not None
    assert execution.result.transactions == 4
    assert execution.result.latency_samples == 4


def test_rejected_measurement_stops_after_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_measurement(*args: object, **kwargs: object) -> MeasurementExecution:
        output_dir = Path(str(args[1]))
        attempt = int(args[5])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "pgbench-prefix").write_text("0 1 1000 0 1 0\n", encoding="utf-8")
        now = datetime.now(UTC).isoformat()
        marker = output_dir / f"measurement-attempt-{attempt}.json"
        result = WorkloadResult(1, 0, 70.0, 1.0, 1.0, 1.0, 60.0, 1)
        marker.write_text(
            json.dumps(
                {
                    "command": ["pgbench"],
                    "stdout": "tps = 70.0",
                    "stderr": "",
                    "runtime_telemetry": {},
                    "result": asdict(result),
                    "started_at": now,
                    "completed_at": now,
                }
            ),
            encoding="utf-8",
        )
        return MeasurementExecution(result, ["pgbench"], "", "", marker, {})

    monkeypatch.setattr("charmdb.workload.run_pgbench_measurement", fake_measurement)
    execution = run_pgbench_promoted_measurement(
        SimpleNamespace(),  # type: ignore[arg-type]
        tmp_path,
        60,
        540,
        32,
        1,
        2,
        1,
        100.0,
        0.8,
    )
    payload = json.loads(execution.marker_path.read_text(encoding="utf-8"))
    assert payload["promotion"]["promoted"] is False
    assert payload["duration_seconds"] == 60
    assert not (tmp_path / "f3-continuation").exists()
