from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.db import connect

TRANSFORM_VERSION = "fingerprint-v1"
FEATURE_NAMES = (
    "select_ratio",
    "update_ratio",
    "insert_ratio",
    "delete_ratio",
    "other_ratio",
    "log_calls_per_second",
    "log_mean_exec_ms",
    "log_rows_per_call",
    "log_shared_hits_per_call",
    "log_shared_reads_per_call",
    "log_wal_bytes_per_call",
)
FEATURE_WEIGHTS = (3.0, 3.0, 2.0, 2.0, 1.0, 0.5, 0.5, 0.25, 0.25, 0.5, 1.0)


@dataclass(frozen=True)
class Fingerprint:
    fingerprint_id: uuid.UUID
    context_id: uuid.UUID
    window_index: int
    raw_features: dict[str, float]
    vector: tuple[float, ...]
    template_distribution: dict[str, float]


def _operation(query: str) -> str:
    normalized = query.lstrip().lower()
    for operation in ("select", "update", "insert", "delete"):
        if normalized.startswith(operation):
            return operation
    return "other"


def build_fingerprint(
    context_id: uuid.UUID,
    window_index: int,
    statements: list[dict[str, Any]],
    duration_seconds: float,
) -> Fingerprint:
    if duration_seconds <= 0:
        raise ValueError("fingerprint duration must be positive")
    total_calls = sum(max(0, int(row["calls"])) for row in statements)
    if total_calls <= 0:
        raise ValueError("fingerprint requires observed query calls")
    operation_calls = {name: 0 for name in ("select", "update", "insert", "delete", "other")}
    totals = {
        "exec_ms": 0.0,
        "rows": 0.0,
        "shared_hits": 0.0,
        "shared_reads": 0.0,
        "wal_bytes": 0.0,
    }
    templates: dict[str, float] = {}
    for row in statements:
        calls = max(0, int(row["calls"]))
        operation_calls[_operation(str(row["query"]))] += calls
        totals["exec_ms"] += float(row["total_exec_time"])
        totals["rows"] += float(row["rows"])
        totals["shared_hits"] += float(row["shared_blks_hit"])
        totals["shared_reads"] += float(row["shared_blks_read"])
        totals["wal_bytes"] += float(row["wal_bytes"])
        templates[str(row["queryid"])] = calls / total_calls
    raw = {
        **{f"{name}_ratio": operation_calls[name] / total_calls for name in operation_calls},
        "calls_per_second": total_calls / duration_seconds,
        "mean_exec_ms": totals["exec_ms"] / total_calls,
        "rows_per_call": totals["rows"] / total_calls,
        "shared_hits_per_call": totals["shared_hits"] / total_calls,
        "shared_reads_per_call": totals["shared_reads"] / total_calls,
        "wal_bytes_per_call": totals["wal_bytes"] / total_calls,
    }
    vector = (
        raw["select_ratio"],
        raw["update_ratio"],
        raw["insert_ratio"],
        raw["delete_ratio"],
        raw["other_ratio"],
        math.log1p(raw["calls_per_second"]),
        math.log1p(raw["mean_exec_ms"]),
        math.log1p(raw["rows_per_call"]),
        math.log1p(raw["shared_hits_per_call"]),
        math.log1p(raw["shared_reads_per_call"]),
        math.log1p(raw["wal_bytes_per_call"]),
    )
    return Fingerprint(uuid.uuid4(), context_id, window_index, raw, vector, templates)


def weighted_distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(FEATURE_NAMES) or len(right) != len(FEATURE_NAMES):
        raise ValueError("fingerprint vectors have incompatible versions")
    numerator = sum(
        weight * (left_value - right_value) ** 2
        for weight, left_value, right_value in zip(FEATURE_WEIGHTS, left, right, strict=True)
    )
    return math.sqrt(numerator / sum(FEATURE_WEIGHTS))


def centroid(fingerprints: list[Fingerprint]) -> tuple[float, ...]:
    if not fingerprints:
        raise ValueError("centroid requires fingerprints")
    return tuple(
        sum(item.vector[index] for item in fingerprints) / len(fingerprints)
        for index in range(len(FEATURE_NAMES))
    )


def reference_threshold(distances: list[float]) -> float:
    if not distances:
        raise ValueError("threshold requires baseline distances")
    mean = sum(distances) / len(distances)
    variance = sum((value - mean) ** 2 for value in distances) / max(1, len(distances) - 1)
    return mean + 4.0 * max(math.sqrt(variance), 0.02)


def persist_fingerprint(
    settings: Settings, fingerprint: Fingerprint, distance: float | None
) -> None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.workload_fingerprints
            (fingerprint_id,context_id,window_index,transform_version,feature_names,
             raw_features,normalized_vector,template_distribution,distance_from_reference)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                fingerprint.fingerprint_id,
                fingerprint.context_id,
                fingerprint.window_index,
                TRANSFORM_VERSION,
                Jsonb(FEATURE_NAMES),
                Jsonb(fingerprint.raw_features),
                Jsonb(fingerprint.vector),
                Jsonb(fingerprint.template_distribution),
                distance,
            ),
        )
        conn.commit()
