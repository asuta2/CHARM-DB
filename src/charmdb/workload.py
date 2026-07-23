from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.db import connect
from charmdb.metrics import capture_snapshot, numeric_difference, percentile


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


def run_pgbench_warmup(
    settings: Settings,
    duration_seconds: int,
    concurrency: int,
    seed: int,
    progress_callback: Callable[[], None] | None = None,
    poll_interval_seconds: float = 5.0,
    application_name: str | None = None,
) -> None:
    if duration_seconds < 0:
        raise ValueError("warm-up duration cannot be negative")
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    if duration_seconds == 0:
        return
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
        str(min(4, concurrency)),
        "-T",
        str(duration_seconds),
        "--random-seed",
        str(seed),
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
        str(min(4, concurrency)),
        "-T",
        str(duration_seconds),
        "--random-seed",
        str(seed),
        "--log",
        f"--log-prefix={log_prefix}",
        "--progress=5",
    ]
    started = time.monotonic()
    completed = _run(
        command,
        settings.target_password,
        duration_seconds + 90,
        progress_callback,
        poll_interval_seconds,
        application_name,
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
        "command": command,
        "result": asdict(result),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    temporary = marker_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(marker_path)
    return MeasurementExecution(result, command, completed.stdout, completed.stderr, marker_path)


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
        for line in path.read_text(encoding="utf-8").splitlines():
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


def _parse_tps(output: str) -> float:
    matches = re.findall(r"tps = ([0-9]+(?:\.[0-9]+)?)", output)
    if not matches:
        raise ValueError("pgbench output did not contain TPS")
    return float(matches[-1])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def benchmark_default(
    settings: Settings,
    active_configuration: dict[str, str] | None = None,
    label: str = "default",
    fidelity: int = 3,
    benchmark_profile: str = "development-default",
) -> tuple[uuid.UUID, Path, WorkloadResult]:
    settings.assert_target_allowed()
    if fidelity not in {2, 3, 4}:
        raise ValueError("executed benchmark fidelity must be F2, F3, or F4")
    if shutil.which("pgbench") is None:
        raise RuntimeError("pgbench is not available on PATH")

    campaign_id = uuid.uuid4()
    trial_id = uuid.uuid4()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    relative_dir = Path("raw") / str(campaign_id) / str(trial_id)
    output_dir = settings.artifact_dir / relative_dir
    output_dir.mkdir(parents=True, exist_ok=False)

    warmup_command = [
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
        str(settings.benchmark_concurrency),
        "-j",
        str(min(4, settings.benchmark_concurrency)),
        "-T",
        str(settings.benchmark_warmup_seconds),
        "--random-seed",
        str(settings.benchmark_seed),
    ]
    if settings.benchmark_warmup_seconds:
        _run(warmup_command, settings.target_password, settings.benchmark_warmup_seconds + 60)

    with connect(settings.target_dsn) as target:
        before = capture_snapshot(target)
        target.commit()

    log_prefix = output_dir / f"pgbench-{stamp}"
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
        str(settings.benchmark_concurrency),
        "-j",
        str(min(4, settings.benchmark_concurrency)),
        "-T",
        str(settings.benchmark_duration_seconds),
        "--random-seed",
        str(settings.benchmark_seed),
        "--log",
        f"--log-prefix={log_prefix}",
        "--progress=5",
    ]
    started = time.monotonic()
    completed = _run(command, settings.target_password, settings.benchmark_duration_seconds + 90)
    elapsed = time.monotonic() - started

    with connect(settings.target_dsn) as target:
        after = capture_snapshot(target)
        target.commit()

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
        "campaign_id": str(campaign_id),
        "trial_id": str(trial_id),
        "benchmark_profile": benchmark_profile,
        "fidelity": fidelity,
        "random_seed": settings.benchmark_seed,
        "warmup_seconds": settings.benchmark_warmup_seconds,
        "measurement_seconds": settings.benchmark_duration_seconds,
        "concurrency": settings.benchmark_concurrency,
        "active_configuration": active_configuration or {},
        "command": command,
        "result": asdict(result),
        "metrics_before": before,
        "metrics_after": after,
        "metric_difference": numeric_difference(before, after),
        "client": {"python": platform.python_version(), "platform": platform.platform()},
        "pgbench_stdout": completed.stdout,
        "pgbench_stderr": completed.stderr,
    }
    result_path = output_dir / "trial.json"
    result_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    with connect(settings.control_dsn) as control:
        with control.cursor() as cur:
            cur.execute(
                """INSERT INTO charm_control.campaigns
                (campaign_id, name, mode, status, objective_definition, constraint_definition)
                VALUES (%s, %s, 'BASELINE', 'COMPLETED', %s, %s)""",
                (
                    campaign_id,
                    f"{label}-{stamp}",
                    Jsonb({"maximize": "throughput_tps"}),
                    Jsonb({"failure_rate_max": 0.0}),
                ),
            )
            cur.execute(
                """INSERT INTO charm_control.trials
                (trial_id, campaign_id, state, benchmark_profile, fidelity, random_seed,
                 active_configuration, objective_values, constraint_values, software_versions,
                 host_snapshot, started_at, completed_at)
                VALUES (%s, %s, 'COMPLETED', %s, %s, %s, %s,
                        %s, %s, %s, '{}'::jsonb, clock_timestamp() - (%s * interval '1 second'),
                        clock_timestamp())""",
                (
                    trial_id,
                    campaign_id,
                    benchmark_profile,
                    fidelity,
                    settings.benchmark_seed,
                    Jsonb(active_configuration or {}),
                    Jsonb({"throughput_tps": result.throughput_tps, "p99_ms": result.p99_ms}),
                    Jsonb({"failures": result.failures}),
                    Jsonb({"server_version": after["server_version"], "charmdb": "0.1.0"}),
                    elapsed,
                ),
            )
            cur.execute(
                """INSERT INTO charm_control.trial_transitions
                (trial_id, from_state, to_state, reason) VALUES
                (%s, NULL, 'CREATED', 'baseline created'),
                (%s, 'CREATED', 'WARMING_UP', 'warm-up started'),
                (%s, 'WARMING_UP', 'RUNNING_FULL_EVALUATION', 'measurement started'),
                (%s, 'RUNNING_FULL_EVALUATION', 'COLLECTING_METRICS', 'workload completed'),
                (%s, 'COLLECTING_METRICS', 'COMPLETED', 'valid metrics persisted')""",
                (trial_id, trial_id, trial_id, trial_id, trial_id),
            )
            for phase, snapshot in (("before", before), ("after", after)):
                cur.execute(
                    """INSERT INTO charm_control.metric_snapshots
                    (snapshot_id, trial_id, phase, source, payload)
                    VALUES (%s, %s, %s, 'postgresql', %s)""",
                    (uuid.uuid4(), trial_id, phase, Jsonb(snapshot)),
                )
            cur.execute(
                """INSERT INTO charm_control.artifacts
                (artifact_id, trial_id, kind, relative_path, sha256, byte_size)
                VALUES (%s, %s, 'trial-json', %s, %s, %s)""",
                (
                    uuid.uuid4(),
                    trial_id,
                    str(relative_dir / "trial.json"),
                    _sha256(result_path),
                    result_path.stat().st_size,
                ),
            )
        control.commit()
    return trial_id, result_path, result
