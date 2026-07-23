from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb
from river.drift import ADWIN

from charmdb.config import Settings
from charmdb.db import connect
from charmdb.fingerprinting import (
    Fingerprint,
    build_fingerprint,
    centroid,
    persist_fingerprint,
    reference_threshold,
    weighted_distance,
)


@dataclass(frozen=True)
class DriftExperimentResult:
    baseline_context_id: uuid.UUID
    drift_context_id: uuid.UUID
    recurring_context_id: uuid.UUID
    threshold: float
    threshold_detected_window: int | None
    adwin_detected_windows: tuple[int, ...]
    recurring_detected: bool
    fingerprints: int


def classify_drift(
    distances: list[float], threshold: float, persistence_windows: int = 2
) -> str | None:
    if threshold <= 0 or persistence_windows < 2:
        raise ValueError("invalid drift-classification parameters")
    above = [value > threshold for value in distances]
    if not any(above):
        return None
    longest = current = 0
    for flag in above:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    if longest < persistence_windows:
        return "TRANSIENT"
    first = above.index(True)
    if first >= 3:
        recent = distances[first - 3 : first + 1]
        increases = [right - left for left, right in pairwise(recent)]
        if all(value > 0 for value in increases) and max(increases) <= threshold * 0.5:
            return "GRADUAL"
    return "SUDDEN"


def _create_context(
    settings: Settings,
    label: str,
    phase_order: int,
    seed: int,
    script: Path,
    windows: int,
    duration_seconds: int,
) -> uuid.UUID:
    context_id = uuid.uuid4()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.workload_contexts
            (context_id,label,phase_order,workload_definition,random_seed)
            VALUES (%s,%s,%s,%s,%s)""",
            (
                context_id,
                label,
                phase_order,
                Jsonb(
                    {
                        "script": str(script),
                        "windows": windows,
                        "window_duration_seconds": duration_seconds,
                        "concurrency": 2,
                    }
                ),
                seed,
            ),
        )
        conn.commit()
    return context_id


def _capture_statements(settings: Settings) -> list[dict[str, Any]]:
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT queryid,query,calls,total_exec_time,rows,shared_blks_hit,
                      shared_blks_read,wal_bytes
               FROM pg_stat_statements
               WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database())
                 AND query NOT ILIKE '%pg_stat_statements%'
                 AND query NOT ILIKE 'EXPLAIN%'
               ORDER BY calls DESC"""
        )
        return [dict(row) for row in cur.fetchall()]


def _run_window(
    settings: Settings,
    context_id: uuid.UUID,
    window_index: int,
    script: Path,
    duration_seconds: int,
    seed: int,
) -> Fingerprint:
    with connect(settings.target_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_stat_statements_reset()")
    env = os.environ.copy()
    env["PGPASSWORD"] = settings.target_password
    completed = subprocess.run(
        [
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
            "2",
            "-j",
            "2",
            "-T",
            str(duration_seconds),
            "--random-seed",
            str(seed),
            "-f",
            str(script),
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=duration_seconds + 30,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"drift workload failed: {completed.stderr.strip()}")
    statements = _capture_statements(settings)
    return build_fingerprint(context_id, window_index, statements, duration_seconds)


def _persist_event(
    settings: Settings,
    fingerprint_id: uuid.UUID,
    detector_type: str,
    score: float,
    threshold: float | None,
    kind: str,
    persistence: int,
    recurring_context_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.drift_events
            (drift_event_id,fingerprint_id,detector_type,score,threshold,drift_kind,
             persistence_windows,recurring_context_id,details)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                uuid.uuid4(),
                fingerprint_id,
                detector_type,
                score,
                threshold,
                kind,
                persistence,
                recurring_context_id,
                Jsonb(details or {}),
            ),
        )
        conn.commit()


def run_drift_experiment(
    settings: Settings,
    baseline_windows: int = 12,
    drift_windows: int = 12,
    recurring_windows: int = 8,
    duration_seconds: int = 1,
    seed: int = 20260713,
) -> DriftExperimentResult:
    if baseline_windows < 8 or drift_windows < 4 or recurring_windows < 2:
        raise ValueError("insufficient windows for controlled drift evaluation")
    read_script = Path("workloads/read_heavy.sql").resolve()
    write_script = Path("workloads/write_heavy.sql").resolve()
    baseline_id = _create_context(
        settings, "read-heavy-baseline", 0, seed, read_script, baseline_windows, duration_seconds
    )
    drift_id = _create_context(
        settings, "write-heavy-drift", 1, seed, write_script, drift_windows, duration_seconds
    )
    recurring_id = _create_context(
        settings, "read-heavy-recurring", 2, seed, read_script, recurring_windows, duration_seconds
    )

    baseline = [
        _run_window(settings, baseline_id, index, read_script, duration_seconds, seed + index)
        for index in range(baseline_windows)
    ]
    reference = centroid(baseline)
    baseline_distances = [weighted_distance(item.vector, reference) for item in baseline]
    threshold = reference_threshold(baseline_distances)
    for item, distance in zip(baseline, baseline_distances, strict=True):
        persist_fingerprint(settings, item, distance)

    adwin = ADWIN(  # type: ignore[no-untyped-call]
        delta=0.01, clock=1, min_window_length=3, grace_period=8
    )
    for distance in baseline_distances:
        adwin.update(distance)  # type: ignore[no-untyped-call]
    global_window = baseline_windows
    threshold_detection: int | None = None
    adwin_windows: list[int] = []
    consecutive = 0

    drift_fingerprints: list[Fingerprint] = []
    drift_distances: list[float] = []
    for index in range(drift_windows):
        item = _run_window(
            settings, drift_id, index, write_script, duration_seconds, seed + global_window
        )
        distance = weighted_distance(item.vector, reference)
        persist_fingerprint(settings, item, distance)
        drift_fingerprints.append(item)
        drift_distances.append(distance)
        consecutive = consecutive + 1 if distance > threshold else 0
        if consecutive >= 2 and threshold_detection is None:
            threshold_detection = global_window
            _persist_event(
                settings,
                item.fingerprint_id,
                "weighted-distance",
                distance,
                threshold,
                classify_drift(drift_distances, threshold) or "SUDDEN",
                consecutive,
                details={"delay_windows": index + 1},
            )
        adwin.update(distance)  # type: ignore[no-untyped-call]
        if adwin.drift_detected:
            adwin_windows.append(global_window)
            _persist_event(
                settings,
                item.fingerprint_id,
                "ADWIN",
                distance,
                None,
                "STATISTICAL_CHANGE",
                1,
                details={"width": adwin.width, "estimation": adwin.estimation},
            )
        global_window += 1

    recurring_detected = False
    recurring_fingerprints: list[Fingerprint] = []
    for index in range(recurring_windows):
        item = _run_window(
            settings, recurring_id, index, read_script, duration_seconds, seed + global_window
        )
        distance = weighted_distance(item.vector, reference)
        persist_fingerprint(settings, item, distance)
        recurring_fingerprints.append(item)
        if not recurring_detected and distance <= threshold:
            recurring_detected = True
            _persist_event(
                settings,
                item.fingerprint_id,
                "mode-similarity",
                distance,
                threshold,
                "RECURRING",
                1,
                recurring_context_id=baseline_id,
            )
        adwin.update(distance)  # type: ignore[no-untyped-call]
        if adwin.drift_detected:
            adwin_windows.append(global_window)
            _persist_event(
                settings,
                item.fingerprint_id,
                "ADWIN",
                distance,
                None,
                "STATISTICAL_CHANGE",
                1,
                details={"width": adwin.width, "estimation": adwin.estimation},
            )
        global_window += 1

    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.workload_contexts SET completed_at=clock_timestamp()
            WHERE context_id=ANY(%s)""",
            ([baseline_id, drift_id, recurring_id],),
        )
        cur.execute(
            """INSERT INTO charm_control.drift_detector_states
            (state_id,detector_type,sequence_number,state) VALUES (%s,'ADWIN',%s,%s)""",
            (
                uuid.uuid4(),
                global_window,
                Jsonb(
                    {
                        "width": adwin.width,
                        "estimation": adwin.estimation,
                        "variance": adwin.variance,
                        "drift_windows": adwin_windows,
                    }
                ),
            ),
        )
        conn.commit()
    return DriftExperimentResult(
        baseline_id,
        drift_id,
        recurring_id,
        threshold,
        threshold_detection,
        tuple(adwin_windows),
        recurring_detected,
        len(baseline) + len(drift_fingerprints) + len(recurring_fingerprints),
    )


def result_json(result: DriftExperimentResult) -> str:
    return json.dumps(
        {
            "baseline_context_id": str(result.baseline_context_id),
            "drift_context_id": str(result.drift_context_id),
            "recurring_context_id": str(result.recurring_context_id),
            "threshold": result.threshold,
            "threshold_detected_window": result.threshold_detected_window,
            "adwin_detected_windows": result.adwin_detected_windows,
            "recurring_detected": result.recurring_detected,
            "fingerprints": result.fingerprints,
        },
        indent=2,
    )
