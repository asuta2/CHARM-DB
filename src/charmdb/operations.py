from __future__ import annotations

import uuid
from typing import Any

from charmdb.config import Settings
from charmdb.db import connect


def _bounded_limit(limit: int) -> int:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    return limit


def drift_history(
    settings: Settings,
    limit: int = 100,
    context_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    limit = _bounded_limit(limit)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.drift_event_id,d.fingerprint_id,f.context_id,c.label AS context_label,
                   f.window_index,d.detector_type,d.score,d.threshold,d.drift_kind,
                   d.persistence_windows,d.recurring_context_id,d.details,d.detected_at
            FROM charm_control.drift_events d
            JOIN charm_control.workload_fingerprints f USING(fingerprint_id)
            JOIN charm_control.workload_contexts c USING(context_id)
            WHERE (%s::uuid IS NULL OR f.context_id=%s)
            ORDER BY d.detected_at DESC,d.drift_event_id
            LIMIT %s
            """,
            (context_id, context_id, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def index_history(settings: Settings, limit: int = 100) -> list[dict[str, Any]]:
    limit = _bounded_limit(limit)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.candidate_id,c.index_name,c.schema_name,c.table_name,c.access_method,
                   c.key_columns,c.include_columns,c.state,c.managed,c.created_at,c.updated_at,
                   e.to_state AS latest_event_state,e.reason AS latest_event_reason,
                   e.occurred_at AS latest_event_at
            FROM charm_control.index_candidates c
            LEFT JOIN LATERAL (
                SELECT to_state,reason,occurred_at
                FROM charm_control.index_lifecycle_events
                WHERE candidate_id=c.candidate_id
                ORDER BY event_id DESC LIMIT 1
            ) e ON true
            ORDER BY c.updated_at DESC,c.candidate_id
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


def index_candidate_history(settings: Settings, candidate_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT candidate_id,definition_hash,index_name,schema_name,table_name,access_method,
                   key_columns,include_columns,predicate_sql,expression_sql,normalized_sql,state,
                   provenance,managed,created_at,updated_at
            FROM charm_control.index_candidates WHERE candidate_id=%s
            """,
            (candidate_id,),
        )
        candidate = cur.fetchone()
        if candidate is None:
            raise ValueError(f"unknown index candidate {candidate_id}")
        cur.execute(
            """
            SELECT event_id,from_state,to_state,reason,details,occurred_at
            FROM charm_control.index_lifecycle_events
            WHERE candidate_id=%s ORDER BY event_id
            """,
            (candidate_id,),
        )
        events = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT measurement_id,build_duration_seconds,build_wal_bytes,index_size_bytes,
                   before_median_ms,after_median_ms,post_drop_median_ms,index_used,
                   paired_query_parameters,created_at
            FROM charm_control.index_measurements
            WHERE candidate_id=%s ORDER BY created_at
            """,
            (candidate_id,),
        )
        measurements = [dict(row) for row in cur.fetchall()]
    result = dict(candidate)
    result["events"] = events
    result["measurements"] = measurements
    return result
