from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg

SNAPSHOT_SQL = """
SELECT jsonb_build_object(
  'database', (SELECT to_jsonb(s) FROM (
    SELECT xact_commit, xact_rollback, blks_read, blks_hit, tup_returned,
           tup_fetched, tup_inserted, tup_updated, tup_deleted, temp_files,
           temp_bytes, deadlocks, checksum_failures, blk_read_time, blk_write_time,
           session_time, active_time, idle_in_transaction_time, sessions,
           sessions_abandoned, sessions_fatal, sessions_killed
    FROM pg_stat_database WHERE datname = current_database()
  ) s),
  'wal', (SELECT to_jsonb(w) FROM (
    SELECT wal_records, wal_fpi, wal_bytes, wal_buffers_full
    FROM pg_stat_wal
  ) w),
  'bgwriter', (SELECT to_jsonb(b) FROM pg_stat_bgwriter b),
  'checkpointer', (SELECT to_jsonb(c) FROM pg_stat_checkpointer c),
  'database_size_bytes', pg_database_size(current_database()),
  'captured_at', clock_timestamp(),
  'server_version', current_setting('server_version')
) AS snapshot
"""


def capture_snapshot(conn: psycopg.Connection[dict[str, Any]]) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(SNAPSHOT_SQL)
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL metric snapshot returned no row")
        return dict(row["snapshot"])


def numeric_difference(before: Any, after: Any) -> Any:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        return {
            key: numeric_difference(before.get(key), after.get(key))
            for key in after
            if key not in {"captured_at", "server_version"}
        }
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return after - before
    return after


def percentile(values: Sequence[int | float], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be in [0, 1]")
    ordered = sorted(float(value) for value in values)
    rank = (len(ordered) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)
