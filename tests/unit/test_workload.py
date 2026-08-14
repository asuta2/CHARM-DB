from pathlib import Path

import pytest

from charmdb.workload import (
    PGBENCH_MAINTENANCE_POLICY,
    _pgbench_maintenance_arguments,
    _resolve_client_threads,
    load_measurement_marker,
    parse_pgbench_log_records,
    parse_pgbench_logs,
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
