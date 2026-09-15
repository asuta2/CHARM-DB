from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from charmdb.config import Settings
from charmdb.metrics import percentile

PGBENCH_MAINTENANCE_POLICY = "canonical-baseline-only-no-vacuum"


@dataclass(frozen=True)
class WorkloadResult:
    transactions: int
    failures: int
    throughput_tps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    duration_seconds: float
    latency_samples: int


@dataclass(frozen=True)
class MeasurementExecution:
    result: WorkloadResult
    command: list[str]
    stdout: str
    stderr: str
    marker_path: Path
    runtime_telemetry: dict[str, Any]


@dataclass(frozen=True)
class PgbenchLogRecord:
    client_id: int
    transaction_number: int
    latency_ms: float
    timestamp_seconds: float
    failed: bool


def _resolve_client_threads(concurrency: int, client_threads: int | None) -> int:
    resolved = min(4, concurrency) if client_threads is None else client_threads
    if resolved < 1 or resolved > concurrency:
        raise ValueError("client threads must be between one and concurrency")
    return resolved


def _pgbench_maintenance_arguments() -> list[str]:
    """Keep startup maintenance out of every timed workload invocation."""
    return ["--no-vacuum"]


def _windows_process_cpu_seconds(process: subprocess.Popen[str]) -> float | None:
    if sys.platform != "win32":
        return None
    import ctypes

    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    handle = getattr(process, "_handle", None)
    if handle is None:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]
    get_process_times.restype = ctypes.c_int
    creation, exit_time, kernel, user = FileTime(), FileTime(), FileTime(), FileTime()
    succeeded = get_process_times(
        ctypes.c_void_p(int(handle)),
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel),
        ctypes.byref(user),
    )
    if not succeeded:
        return None

    def seconds(value: FileTime) -> float:
        ticks = (int(value.high) << 32) | int(value.low)
        return ticks / 10_000_000

    return seconds(kernel) + seconds(user)


def _run_profiled(
    command: list[str],
    password: str,
    timeout: int,
    client_threads: int,
    progress_callback: Callable[[], None] | None,
    poll_interval_seconds: float,
    application_name: str | None,
    runtime_sample_callback: Callable[[], dict[str, Any]] | None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    if poll_interval_seconds <= 0:
        raise ValueError("poll interval must be positive")
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    if application_name is not None:
        if not application_name.startswith("charmdb:") or len(application_name) > 63:
            raise ValueError("workload application_name must be a bounded CHARM-DB identity")
        env["PGAPPNAME"] = application_name
    child_times_before = os.times()
    process = subprocess.Popen(
        command,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    windows_cpu_before = _windows_process_cpu_seconds(process)
    started = time.monotonic()
    samples: list[dict[str, Any]] = []
    deadline = started + timeout

    def sample() -> None:
        if runtime_sample_callback is None:
            return
        payload = runtime_sample_callback()
        samples.append({"elapsed_seconds": time.monotonic() - started, **payload})

    try:
        sample()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(poll_interval_seconds, remaining))
            except subprocess.TimeoutExpired:
                if progress_callback is not None:
                    progress_callback()
                sample()
                continue
            sample()
            completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            completed.check_returncode()
            break
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    wall_seconds = time.monotonic() - started
    windows_cpu_after = _windows_process_cpu_seconds(process)
    child_times_after = os.times()
    if windows_cpu_before is not None and windows_cpu_after is not None:
        cpu_seconds = max(0.0, windows_cpu_after - windows_cpu_before)
        cpu_method = "windows-get-process-times"
    else:
        cpu_seconds = max(
            0.0,
            (child_times_after.children_user + child_times_after.children_system)
            - (child_times_before.children_user + child_times_before.children_system),
        )
        cpu_method = "os-child-times"
    logical_cpus = os.cpu_count() or 1
    telemetry = {
        "client_cpu_seconds": cpu_seconds,
        "wall_seconds": wall_seconds,
        "client_threads": client_threads,
        "host_logical_cpus": logical_cpus,
        "client_cpu_percent_of_thread_capacity": (
            100.0 * cpu_seconds / max(wall_seconds * client_threads, 1e-9)
        ),
        "client_cpu_percent_of_host_capacity": (
            100.0 * cpu_seconds / max(wall_seconds * logical_cpus, 1e-9)
        ),
        "client_cpu_sampling_method": cpu_method,
        "runtime_samples": samples,
    }
    return completed, telemetry


def run_pgbench_warmup(
    settings: Settings,
    duration_seconds: int,
    concurrency: int,
    seed: int,
    progress_callback: Callable[[], None] | None = None,
    poll_interval_seconds: float = 5.0,
    application_name: str | None = None,
    client_threads: int | None = None,
) -> None:
    if duration_seconds < 0:
        raise ValueError("warm-up duration cannot be negative")
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    if duration_seconds == 0:
        return
    resolved_threads = _resolve_client_threads(concurrency, client_threads)
    command = [
        "pgbench",
        "-h",
        settings.target_host,
        "-p",
        str(settings.target_port),
        "-U",
        settings.target_user,
        "-d",
        settings.target_db,
        "-c",
        str(concurrency),
        "-j",
        str(resolved_threads),
        "-T",
        str(duration_seconds),
        "--random-seed",
        str(seed),
        *_pgbench_maintenance_arguments(),
    ]
    _run(
        command,
        settings.target_password,
        duration_seconds + 60,
        progress_callback,
        poll_interval_seconds,
        application_name,
    )


def load_measurement_marker(path: Path) -> MeasurementExecution:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result_payload = payload.get("result")
    command = payload.get("command")
    if not isinstance(result_payload, dict) or not isinstance(command, list):
        raise ValueError(f"invalid measurement marker {path}")
    result = WorkloadResult(
        transactions=int(result_payload["transactions"]),
        failures=int(result_payload["failures"]),
        throughput_tps=float(result_payload["throughput_tps"]),
        p50_ms=float(result_payload["p50_ms"]),
        p95_ms=float(result_payload["p95_ms"]),
        p99_ms=float(result_payload["p99_ms"]),
        duration_seconds=float(result_payload["duration_seconds"]),
        latency_samples=int(result_payload["latency_samples"]),
    )
    return MeasurementExecution(
        result=result,
        command=[str(item) for item in command],
        stdout=str(payload.get("stdout", "")),
        stderr=str(payload.get("stderr", "")),
        marker_path=path,
        runtime_telemetry=dict(payload.get("runtime_telemetry") or {}),
    )


def run_pgbench_measurement(
    settings: Settings,
    output_dir: Path,
    duration_seconds: int,
    concurrency: int,
    seed: int,
    attempt: int,
    progress_callback: Callable[[], None] | None = None,
    poll_interval_seconds: float = 5.0,
    application_name: str | None = None,
    client_threads: int | None = None,
    runtime_sample_callback: Callable[[], dict[str, Any]] | None = None,
) -> MeasurementExecution:
    settings.assert_target_allowed()
    if shutil.which("pgbench") is None:
        raise RuntimeError("pgbench is not available on PATH")
    if duration_seconds < 1 or concurrency < 1 or attempt < 1:
        raise ValueError("duration, concurrency, and attempt must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    marker_path = output_dir / f"measurement-attempt-{attempt}.json"
    if marker_path.exists():
        return load_measurement_marker(marker_path)
    resolved_threads = _resolve_client_threads(concurrency, client_threads)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    log_prefix = output_dir / f"pgbench-{stamp}-attempt-{attempt}"
    command = [
        "pgbench",
        "-h",
        settings.target_host,
        "-p",
        str(settings.target_port),
        "-U",
        settings.target_user,
        "-d",
        settings.target_db,
        "-c",
        str(concurrency),
        "-j",
        str(resolved_threads),
        "-T",
        str(duration_seconds),
        "--random-seed",
        str(seed),
        *_pgbench_maintenance_arguments(),
        "--log",
        f"--log-prefix={log_prefix}",
        "--progress=5",
    ]
    started_at = datetime.now(UTC)
    started = time.monotonic()
    completed, runtime_telemetry = _run_profiled(
        command,
        settings.target_password,
        duration_seconds + 90,
        resolved_threads,
        progress_callback,
        poll_interval_seconds,
        application_name,
        runtime_sample_callback,
    )
    elapsed = time.monotonic() - started
    log_paths = sorted(output_dir.glob(f"{log_prefix.name}*"))
    latencies, transactions, failures = parse_pgbench_logs(log_paths)
    if not latencies:
        raise RuntimeError("pgbench produced no usable latency samples")
    result = WorkloadResult(
        transactions=transactions,
        failures=failures,
        throughput_tps=_parse_tps(completed.stdout + completed.stderr),
        p50_ms=percentile(latencies, 0.50),
        p95_ms=percentile(latencies, 0.95),
        p99_ms=percentile(latencies, 0.99),
        duration_seconds=elapsed,
        latency_samples=len(latencies),
    )
    payload = {
        "attempt": attempt,
        "seed": seed,
        "duration_seconds": duration_seconds,
        "concurrency": concurrency,
        "pgbench_maintenance_policy": PGBENCH_MAINTENANCE_POLICY,
        "command": command,
        "result": asdict(result),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "runtime_telemetry": runtime_telemetry,
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
    }
    temporary = marker_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(marker_path)
    return MeasurementExecution(
        result,
        command,
        completed.stdout,
        completed.stderr,
        marker_path,
        runtime_telemetry,
    )


def run_pgbench_promoted_measurement(
    settings: Settings,
    output_dir: Path,
    prefix_seconds: int,
    continuation_seconds: int,
    concurrency: int,
    seed: int,
    continuation_seed: int,
    attempt: int,
    baseline_throughput_tps: float,
    throughput_floor_ratio: float,
    progress_callback: Callable[[], None] | None = None,
    poll_interval_seconds: float = 5.0,
    application_name: str | None = None,
    client_threads: int | None = None,
    runtime_sample_callback: Callable[[], dict[str, Any]] | None = None,
) -> MeasurementExecution:
    """Run an atomic F2 decision followed by an optional same-state continuation.

    The two pgbench client processes intentionally use independent markers.  A
    recovery therefore never repeats a completed prefix or continuation.  No
    database restore, restart, or warm-up occurs between these calls.
    """
    if prefix_seconds < 1 or continuation_seconds < 1:
        raise ValueError("promotion stages must have positive durations")
    if baseline_throughput_tps <= 0 or not 0 < throughput_floor_ratio <= 1:
        raise ValueError("promotion baseline and throughput ratio must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    marker_path = output_dir / f"measurement-attempt-{attempt}.json"
    if marker_path.exists():
        return load_measurement_marker(marker_path)

    prefix_dir = output_dir / "f2-prefix"
    continuation_dir = output_dir / "f3-continuation"
    prefix_application = f"{application_name}-f2" if application_name else None
    continuation_application = f"{application_name}-f3" if application_name else None
    prefix = run_pgbench_measurement(
        settings,
        prefix_dir,
        prefix_seconds,
        concurrency,
        seed,
        attempt,
        progress_callback,
        poll_interval_seconds,
        prefix_application,
        client_threads,
        runtime_sample_callback,
    )
    threshold_tps = baseline_throughput_tps * throughput_floor_ratio
    promoted = prefix.result.failures == 0 and prefix.result.throughput_tps >= threshold_tps
    continuation: MeasurementExecution | None = None
    if promoted:
        continuation = run_pgbench_measurement(
            settings,
            continuation_dir,
            continuation_seconds,
            concurrency,
            continuation_seed,
            attempt,
            progress_callback,
            poll_interval_seconds,
            continuation_application,
            client_threads,
            runtime_sample_callback,
        )
    reconnect_gap_seconds = 0.0
    if continuation is not None:
        prefix_marker = json.loads(prefix.marker_path.read_text(encoding="utf-8"))
        continuation_marker = json.loads(continuation.marker_path.read_text(encoding="utf-8"))
        prefix_completed = datetime.fromisoformat(str(prefix_marker["completed_at"]))
        continuation_started = datetime.fromisoformat(str(continuation_marker["started_at"]))
        reconnect_gap_seconds = max(
            0.0, (continuation_started - prefix_completed).total_seconds()
        )

    stage_executions: list[MeasurementExecution] = [prefix]
    if continuation is not None:
        stage_executions.append(continuation)
    log_paths = sorted(prefix_dir.glob("pgbench-*"))
    if continuation is not None:
        log_paths.extend(sorted(continuation_dir.glob("pgbench-*")))
    latencies, transactions, failures = parse_pgbench_logs(log_paths)
    if not latencies:
        raise RuntimeError("promoted measurement produced no usable latency samples")
    planned_seconds = prefix_seconds + (continuation_seconds if promoted else 0)
    result = WorkloadResult(
        transactions=transactions,
        failures=failures,
        throughput_tps=(transactions - failures) / planned_seconds,
        p50_ms=percentile(latencies, 0.50),
        p95_ms=percentile(latencies, 0.95),
        p99_ms=percentile(latencies, 0.99),
        duration_seconds=sum(item.result.duration_seconds for item in stage_executions),
        latency_samples=len(latencies),
    )
    promotion = {
        "promoted": promoted,
        "prefix_seconds": prefix_seconds,
        "continuation_seconds": continuation_seconds if promoted else 0,
        "prefix_throughput_tps": prefix.result.throughput_tps,
        "prefix_failures": prefix.result.failures,
        "baseline_throughput_tps": baseline_throughput_tps,
        "throughput_floor_ratio": throughput_floor_ratio,
        "threshold_tps": threshold_tps,
        "reconnect_gap_seconds": reconnect_gap_seconds,
        "prefix_marker": str(prefix.marker_path.relative_to(output_dir)),
        "continuation_marker": (
            str(continuation.marker_path.relative_to(output_dir))
            if continuation is not None
            else None
        ),
    }
    runtime_telemetry = {
        "promotion": promotion,
        "f2": prefix.runtime_telemetry,
        "f3_continuation": (
            continuation.runtime_telemetry if continuation is not None else None
        ),
    }
    command = [item for stage in stage_executions for item in stage.command]
    stdout = "\n".join(item.stdout for item in stage_executions)
    stderr = "\n".join(item.stderr for item in stage_executions)
    payload = {
        "attempt": attempt,
        "seed": seed,
        "continuation_seed": continuation_seed,
        "duration_seconds": planned_seconds,
        "concurrency": concurrency,
        "pgbench_maintenance_policy": PGBENCH_MAINTENANCE_POLICY,
        "command": command,
        "result": asdict(result),
        "stdout": stdout,
        "stderr": stderr,
        "runtime_telemetry": runtime_telemetry,
        "promotion": promotion,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    temporary = marker_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(marker_path)
    return MeasurementExecution(
        result,
        command,
        stdout,
        stderr,
        marker_path,
        runtime_telemetry,
    )


def _run(
    command: list[str],
    password: str,
    timeout: int,
    progress_callback: Callable[[], None] | None = None,
    poll_interval_seconds: float = 5.0,
    application_name: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    if application_name is not None:
        if not application_name.startswith("charmdb:") or len(application_name) > 63:
            raise ValueError("workload application_name must be a bounded CHARM-DB identity")
        env["PGAPPNAME"] = application_name
    if progress_callback is None:
        return subprocess.run(
            command,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=True,
        )
    if poll_interval_seconds <= 0:
        raise ValueError("poll interval must be positive")
    process = subprocess.Popen(
        command,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(poll_interval_seconds, remaining))
            except subprocess.TimeoutExpired:
                progress_callback()
                continue
            completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            completed.check_returncode()
            return completed
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def seed_pgbench(settings: Settings, scale: int) -> str:
    settings.assert_target_allowed()
    if scale < 1:
        raise ValueError("scale must be positive")
    command = [
        "pgbench",
        "-h",
        settings.target_host,
        "-p",
        str(settings.target_port),
        "-U",
        settings.target_user,
        "-d",
        settings.target_db,
        "-i",
        "-s",
        str(scale),
        "--foreign-keys",
    ]
    return _run(command, settings.target_password, timeout=max(300, scale * 60)).stdout


def parse_pgbench_logs(paths: list[Path]) -> tuple[list[float], int, int]:
    latencies_ms: list[float] = []
    failures = 0
    transactions = 0
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line or line.startswith("#"):
                    continue
                fields = line.split()
                if len(fields) < 3:
                    continue
                try:
                    latency_us = int(fields[2])
                except ValueError:
                    failures += 1
                    continue
                transactions += 1
                if latency_us >= 0:
                    latencies_ms.append(latency_us / 1000.0)
                else:
                    failures += 1
    return latencies_ms, transactions, failures


def parse_pgbench_log_records(paths: list[Path]) -> list[PgbenchLogRecord]:
    records: list[PgbenchLogRecord] = []
    for path in paths:
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
                except ValueError:
                    continue
                timestamp_seconds = 0.0
                if len(fields) >= 6:
                    try:
                        timestamp_seconds = int(fields[4]) + int(fields[5]) / 1_000_000.0
                    except ValueError:
                        timestamp_seconds = 0.0
                failed = latency_us < 0
                records.append(
                    PgbenchLogRecord(
                        client_id=client_id,
                        transaction_number=transaction_number,
                        latency_ms=max(0, latency_us) / 1000.0,
                        timestamp_seconds=timestamp_seconds,
                        failed=failed,
                    )
                )
    return records


def _parse_tps(output: str) -> float:
    matches = re.findall(r"tps = ([0-9]+(?:\.[0-9]+)?)", output)
    if not matches:
        raise ValueError("pgbench output did not contain TPS")
    return float(matches[-1])
