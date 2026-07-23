from pathlib import Path

from charmdb.workload import load_measurement_marker, parse_pgbench_logs


def test_parse_pgbench_logs(tmp_path: Path) -> None:
    log = tmp_path / "pgbench_log.1"
    log.write_text("0 1 1500 0 1\n0 2 2500 0 2\n", encoding="utf-8")
    latencies, transactions, failures = parse_pgbench_logs([log])
    assert latencies == [1.5, 2.5]
    assert transactions == 2
    assert failures == 0


def test_atomic_measurement_marker_can_reconstruct_result(tmp_path: Path) -> None:
    marker = tmp_path / "measurement-attempt-2.json"
    marker.write_text(
        """{
          "command": ["pgbench", "-T", "5"],
          "stdout": "tps = 123.4",
          "stderr": "",
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
