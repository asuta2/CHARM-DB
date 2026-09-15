from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from scipy.stats import spearmanr  # type: ignore[import-untyped]

from charmdb.metrics import percentile
from charmdb.workload import PgbenchLogRecord, parse_pgbench_log_records


@dataclass(frozen=True)
class WindowMetrics:
    start_seconds: float
    end_seconds: float
    transactions: int
    failures: int
    throughput_tps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float


def _measurement_start_epoch(payload: dict[str, Any]) -> float:
    started_at = payload.get("started_at")
    if isinstance(started_at, str):
        return datetime.fromisoformat(started_at).timestamp()
    completed_at = payload.get("completed_at")
    result = payload.get("result")
    if not isinstance(completed_at, str) or not isinstance(result, dict):
        raise ValueError("measurement marker has no usable timing boundary")
    return datetime.fromisoformat(completed_at).timestamp() - float(result["duration_seconds"])


def _log_paths(marker_path: Path, command: list[Any]) -> list[Path]:
    prefix_argument = next(
        (str(item) for item in command if str(item).startswith("--log-prefix=")),
        None,
    )
    if prefix_argument is None:
        raise ValueError("measurement marker command has no pgbench log prefix")
    prefix = Path(prefix_argument.split("=", 1)[1])
    paths = sorted(marker_path.parent.glob(f"{prefix.name}*"))
    if not paths:
        raise ValueError(f"measurement marker has no logs for prefix {prefix.name}")
    return paths


def calculate_window(
    records: list[PgbenchLogRecord],
    measurement_start_epoch: float,
    start_seconds: float,
    end_seconds: float,
) -> WindowMetrics:
    if start_seconds < 0 or end_seconds <= start_seconds:
        raise ValueError("window boundaries must be positive and increasing")
    start_epoch = measurement_start_epoch + start_seconds
    end_epoch = measurement_start_epoch + end_seconds
    selected = [
        record
        for record in records
        if record.timestamp_seconds > 0 and start_epoch <= record.timestamp_seconds < end_epoch
    ]
    successful = [record.latency_ms for record in selected if not record.failed]
    if not successful:
        raise ValueError(f"window {start_seconds}-{end_seconds} has no successful transactions")
    duration = end_seconds - start_seconds
    return WindowMetrics(
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        transactions=len(selected),
        failures=sum(record.failed for record in selected),
        throughput_tps=len(successful) / duration,
        p50_ms=percentile(successful, 0.50),
        p95_ms=percentile(successful, 0.95),
        p99_ms=percentile(successful, 0.99),
    )


def analyze_measurement_marker(
    marker_path: Path,
    *,
    prefix_seconds: int = 300,
    rolling_window_seconds: int = 60,
) -> dict[str, Any]:
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    command = payload.get("command")
    result = payload.get("result")
    if not isinstance(command, list) or not isinstance(result, dict):
        raise ValueError("invalid measurement marker")
    duration = float(payload.get("duration_seconds") or result["duration_seconds"])
    if duration < prefix_seconds * 2:
        raise ValueError("measurement must contain both first and second prefix windows")
    start_epoch = _measurement_start_epoch(payload)
    records = parse_pgbench_log_records(_log_paths(marker_path, command))
    if any(record.timestamp_seconds <= 0 for record in records):
        raise ValueError("pgbench logs do not contain epoch timestamps")
    first = calculate_window(records, start_epoch, 0.0, float(prefix_seconds))
    second = calculate_window(
        records,
        start_epoch,
        float(prefix_seconds),
        float(prefix_seconds * 2),
    )
    full = calculate_window(records, start_epoch, 0.0, float(prefix_seconds * 2))
    rolling = [
        calculate_window(
            records,
            start_epoch,
            float(start),
            float(start + rolling_window_seconds),
        )
        for start in range(0, prefix_seconds * 2, rolling_window_seconds)
    ]
    return {
        "marker_path": str(marker_path),
        "measurement_start_epoch": start_epoch,
        "prefix_seconds": prefix_seconds,
        "first_window": asdict(first),
        "second_window": asdict(second),
        "full_window": asdict(full),
        "rolling_windows": [asdict(item) for item in rolling],
        "persisted_result": result,
        "runtime_telemetry": dict(payload.get("runtime_telemetry") or {}),
    }


def analyze_rolling_measurement(
    marker_path: Path,
    *,
    rolling_window_seconds: int = 60,
) -> dict[str, Any]:
    """Analyze every complete fixed-width window in a measurement marker."""
    if rolling_window_seconds < 1:
        raise ValueError("rolling window must be positive")
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    command = payload.get("command")
    result = payload.get("result")
    if not isinstance(command, list) or not isinstance(result, dict):
        raise ValueError("invalid measurement marker")
    duration = float(payload.get("duration_seconds") or result["duration_seconds"])
    complete_duration = int(duration // rolling_window_seconds) * rolling_window_seconds
    if complete_duration < rolling_window_seconds:
        raise ValueError("measurement is shorter than one rolling window")
    start_epoch = _measurement_start_epoch(payload)
    records = parse_pgbench_log_records(_log_paths(marker_path, command))
    if any(record.timestamp_seconds <= 0 for record in records):
        raise ValueError("pgbench logs do not contain epoch timestamps")
    rolling = [
        calculate_window(records, start_epoch, float(start), float(start + rolling_window_seconds))
        for start in range(0, complete_duration, rolling_window_seconds)
    ]
    return {
        "marker_path": str(marker_path),
        "measurement_start_epoch": start_epoch,
        "measurement_seconds": duration,
        "rolling_window_seconds": rolling_window_seconds,
        "rolling_windows": [asdict(item) for item in rolling],
        "persisted_result": result,
        "runtime_telemetry": dict(payload.get("runtime_telemetry") or {}),
    }


def analyze_prefix_windows(
    marker_path: Path, window_seconds: tuple[int, ...]
) -> dict[int, WindowMetrics]:
    """Calculate cumulative measurement prefixes from one authenticated marker.

    The caller authenticates the marker bytes against durable evidence. This
    function parses the transaction logs once and returns only windows that fit
    inside the persisted measurement duration.
    """
    if not window_seconds or any(window < 1 for window in window_seconds):
        raise ValueError("prefix windows must contain positive seconds")
    if len(set(window_seconds)) != len(window_seconds):
        raise ValueError("prefix windows must be unique")
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    command = payload.get("command")
    result = payload.get("result")
    if not isinstance(command, list) or not isinstance(result, dict):
        raise ValueError("invalid measurement marker")
    duration = float(payload.get("duration_seconds") or result["duration_seconds"])
    if max(window_seconds) > duration:
        raise ValueError("prefix window exceeds persisted measurement duration")
    start_epoch = _measurement_start_epoch(payload)
    end_epoch = start_epoch + max(window_seconds)
    records: list[PgbenchLogRecord] = []
    for path in _log_paths(marker_path, command):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line or line.startswith("#"):
                    continue
                fields = line.split()
                if len(fields) < 3:
                    continue
                try:
                    client_id = int(fields[0])
                    transaction_number = int(fields[1])
                    latency_us = int(fields[2])
                    timestamp = (
                        int(fields[4]) + int(fields[5]) / 1_000_000.0
                        if len(fields) >= 6
                        else 0.0
                    )
                except ValueError:
                    continue
                if timestamp >= end_epoch:
                    break
                records.append(
                    PgbenchLogRecord(
                        client_id=client_id,
                        transaction_number=transaction_number,
                        latency_ms=max(0, latency_us) / 1000.0,
                        timestamp_seconds=timestamp,
                        failed=latency_us < 0,
                    )
                )
    if any(record.timestamp_seconds <= 0 for record in records):
        raise ValueError("pgbench logs do not contain epoch timestamps")
    return {
        window: calculate_window(records, start_epoch, 0.0, float(window))
        for window in sorted(window_seconds)
    }


def _pareto_names(rows: list[dict[str, Any]], metric_key: str) -> set[str]:
    result: set[str] = set()
    for candidate in rows:
        metrics = dict(candidate[metric_key])
        dominated = any(
            other is not candidate
            and float(other[metric_key]["throughput_tps"]) >= float(metrics["throughput_tps"])
            and float(other[metric_key]["p99_ms"]) <= float(metrics["p99_ms"])
            and (
                float(other[metric_key]["throughput_tps"]) > float(metrics["throughput_tps"])
                or float(other[metric_key]["p99_ms"]) < float(metrics["p99_ms"])
            )
            for other in rows
        )
        if not dominated:
            result.add(str(candidate["configuration_name"]))
    return result


def _aggregate_configurations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = sorted({str(row["configuration_name"]) for row in rows})
    aggregated: list[dict[str, Any]] = []
    for name in names:
        members = [row for row in rows if str(row["configuration_name"]) == name]
        item: dict[str, Any] = {"configuration_name": name}
        for metric_key in ("first_window", "second_window", "full_window"):
            item[metric_key] = {
                objective: statistics.median(
                    float(member[metric_key][objective]) for member in members
                )
                for objective in ("throughput_tps", "p99_ms")
            }
        aggregated.append(item)
    return aggregated


def compare_duration_windows(
    rows: list[dict[str, Any]],
    *,
    maximum_tps_relative_difference: float = 0.05,
    maximum_p99_absolute_difference_ms: float = 1.0,
    minimum_rank_correlation: float = 0.90,
    minimum_pareto_membership_agreement: float = 0.80,
) -> dict[str, Any]:
    if len(rows) < 3:
        raise ValueError("duration comparison requires at least three paired rows")
    for row in rows:
        for key in ("configuration_name", "first_window", "second_window", "full_window"):
            if key not in row:
                raise ValueError(f"duration comparison row is missing {key}")

    paired_first_tps = [float(row["first_window"]["throughput_tps"]) for row in rows]
    paired_second_tps = [float(row["second_window"]["throughput_tps"]) for row in rows]
    paired_full_tps = [float(row["full_window"]["throughput_tps"]) for row in rows]
    paired_first_p99 = [float(row["first_window"]["p99_ms"]) for row in rows]
    paired_second_p99 = [float(row["second_window"]["p99_ms"]) for row in rows]
    paired_full_p99 = [float(row["full_window"]["p99_ms"]) for row in rows]

    median_tps_relative = statistics.median(
        abs(prefix - full) / max(abs(full), 1e-9)
        for prefix, full in zip(paired_first_tps, paired_full_tps, strict=True)
    )
    median_p99_absolute = statistics.median(
        abs(prefix - full)
        for prefix, full in zip(paired_first_p99, paired_full_p99, strict=True)
    )
    late_tps_relative = statistics.median(
        abs(first - second) / max(abs(first), 1e-9)
        for first, second in zip(paired_first_tps, paired_second_tps, strict=True)
    )
    late_p99_absolute = statistics.median(
        abs(first - second)
        for first, second in zip(paired_first_p99, paired_second_p99, strict=True)
    )
    aggregated = _aggregate_configurations(rows)
    first_tps = [float(row["first_window"]["throughput_tps"]) for row in aggregated]
    second_tps = [float(row["second_window"]["throughput_tps"]) for row in aggregated]
    full_tps = [float(row["full_window"]["throughput_tps"]) for row in aggregated]
    first_p99 = [float(row["first_window"]["p99_ms"]) for row in aggregated]
    second_p99 = [float(row["second_window"]["p99_ms"]) for row in aggregated]
    full_p99 = [float(row["full_window"]["p99_ms"]) for row in aggregated]
    tps_rank = float(spearmanr(first_tps, full_tps).statistic)
    p99_rank = float(spearmanr(first_p99, full_p99).statistic)
    late_tps_rank = float(spearmanr(first_tps, second_tps).statistic)
    late_p99_rank = float(spearmanr(first_p99, second_p99).statistic)
    first_pareto = _pareto_names(aggregated, "first_window")
    second_pareto = _pareto_names(aggregated, "second_window")
    full_pareto = _pareto_names(aggregated, "full_window")
    first_agreement = sum(
        (str(row["configuration_name"]) in first_pareto)
        == (str(row["configuration_name"]) in full_pareto)
        for row in aggregated
    ) / len(aggregated)
    late_agreement = sum(
        (str(row["configuration_name"]) in first_pareto)
        == (str(row["configuration_name"]) in second_pareto)
        for row in aggregated
    ) / len(aggregated)
    passed = all(
        (
            median_tps_relative <= maximum_tps_relative_difference,
            median_p99_absolute <= maximum_p99_absolute_difference_ms,
            tps_rank >= minimum_rank_correlation,
            p99_rank >= minimum_rank_correlation,
            first_agreement >= minimum_pareto_membership_agreement,
            late_tps_relative <= maximum_tps_relative_difference,
            late_p99_absolute <= maximum_p99_absolute_difference_ms,
            late_tps_rank >= minimum_rank_correlation,
            late_p99_rank >= minimum_rank_correlation,
            late_agreement >= minimum_pareto_membership_agreement,
        )
    )
    return {
        "paired_rows": len(rows),
        "configurations": len(aggregated),
        "median_tps_relative_difference": median_tps_relative,
        "median_p99_absolute_difference_ms": median_p99_absolute,
        "tps_rank_correlation": tps_rank,
        "p99_rank_correlation": p99_rank,
        "pareto_membership_agreement": first_agreement,
        "late_window_tps_relative_difference": late_tps_relative,
        "late_window_p99_absolute_difference_ms": late_p99_absolute,
        "late_window_tps_rank_correlation": late_tps_rank,
        "late_window_p99_rank_correlation": late_p99_rank,
        "late_window_pareto_membership_agreement": late_agreement,
        "first_window_pareto": sorted(first_pareto),
        "second_window_pareto": sorted(second_pareto),
        "full_window_pareto": sorted(full_pareto),
        "passed": passed,
    }
