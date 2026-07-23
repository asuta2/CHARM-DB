from __future__ import annotations

from typing import Any

from charmdb.config import Settings
from charmdb.db import connect


def _labels(values: dict[str, Any]) -> str:
    if not values:
        return ""
    encoded = []
    for name, value in sorted(values.items()):
        text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        encoded.append(f'{name}="{text}"')
    return "{" + ",".join(encoded) + "}"


def render_prometheus_metrics(settings: Settings) -> str:
    lines = [
        "# HELP charmdb_campaigns Persisted CHARM-DB campaigns by status.",
        "# TYPE charmdb_campaigns gauge",
    ]
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT status,count(*) AS count FROM charm_control.campaigns GROUP BY status")
        for row in cur.fetchall():
            lines.append(f"charmdb_campaigns{_labels({'status': row['status']})} {row['count']}")
        lines.extend(
            [
                "# HELP charmdb_trials Persisted CHARM-DB trials by workflow and state.",
                "# TYPE charmdb_trials gauge",
            ]
        )
        cur.execute(
            """SELECT workflow_kind,state,count(*) AS count FROM charm_control.trials
               GROUP BY workflow_kind,state"""
        )
        for row in cur.fetchall():
            labels = _labels({"state": row["state"], "workflow": row["workflow_kind"]})
            lines.append(f"charmdb_trials{labels} {row['count']}")
        scalar_queries = {
            "charmdb_active_leases": """SELECT count(*) AS count FROM charm_control.trials
                WHERE completed_at IS NULL AND lease_expires_at > clock_timestamp()""",
            "charmdb_expired_leases": """SELECT count(*) AS count FROM charm_control.trials
                WHERE completed_at IS NULL AND lease_expires_at <= clock_timestamp()""",
            "charmdb_artifacts": "SELECT count(*) AS count FROM charm_control.artifacts",
            "charmdb_verified_rollbacks": (
                "SELECT count(*) AS count FROM charm_control.rollbacks WHERE verified"
            ),
        }
        for metric, query in scalar_queries.items():
            cur.execute(query)
            scalar_row = cur.fetchone()
            lines.extend(
                [
                    f"# TYPE {metric} gauge",
                    f"{metric} {int(scalar_row['count']) if scalar_row is not None else 0}",
                ]
            )
    target_ready = 0
    target_size = 0
    try:
        with connect(settings.target_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT (NOT pg_is_in_recovery())::integer AS ready,
                          pg_database_size(current_database())::bigint AS bytes"""
            )
            target_row = cur.fetchone()
            if target_row is not None:
                target_ready = int(target_row["ready"])
                target_size = int(target_row["bytes"])
    except Exception:
        target_ready = 0
    lines.extend(
        [
            "# HELP charmdb_target_ready Whether the configured target answered a writable check.",
            "# TYPE charmdb_target_ready gauge",
            f"charmdb_target_ready {target_ready}",
            "# TYPE charmdb_target_database_bytes gauge",
            f"charmdb_target_database_bytes {target_size}",
        ]
    )
    return "\n".join(lines) + "\n"
