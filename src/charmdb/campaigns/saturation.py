from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.controller import apply_configuration, discover_knobs, rollback_configuration
from charmdb.db import connect
from charmdb.protocol import load_manifest
from charmdb.resources import capture_and_validate_resources
from charmdb.restore.candidate import PGBENCH_RELATIONS, standardize_logical_restore_state
from charmdb.restore.preflight import _target_safety
from charmdb.worker import (
    SATURATION_PHASE1_WORKFLOW,
    control_campaign,
    create_campaign,
    run_once,
)
from charmdb.workload import seed_pgbench

PHASE1_SCALES = (100, 250, 500)
PHASE1_CONCURRENCY = (4, 8, 16, 32, 64)
PHASE1_ANCHOR_ORDER = (
    "postgresql_default",
    "throughput_leaning",
    "latency_leaning",
)
PHASE1_WARMUP_SECONDS = 60
PHASE1_MEASUREMENT_SECONDS = 120
PHASE1_CLIENT_THREAD_CAP = 4
PHASE1_MANIFEST = Path("experiments/thesis/manifests/sanity-and-saturation.json")

PHASE1_ANCHORS: dict[str, dict[str, str]] = {
    "postgresql_default": {
        "shared_buffers": "16384",
        "effective_cache_size": "524288",
        "work_mem": "4096",
        "maintenance_work_mem": "65536",
        "checkpoint_timeout": "300",
        "checkpoint_completion_target": "0.9",
        "max_wal_size": "1024",
        "random_page_cost": "4",
        "effective_io_concurrency": "16",
        "default_statistics_target": "100",
        "max_parallel_workers_per_gather": "2",
    },
    "throughput_leaning": {
        "shared_buffers": "183500",
        "effective_cache_size": "524288",
        "work_mem": "16384",
        "maintenance_work_mem": "262144",
        "checkpoint_timeout": "900",
        "checkpoint_completion_target": "0.95",
        "max_wal_size": "4096",
        "random_page_cost": "1.1",
        "effective_io_concurrency": "200",
        "default_statistics_target": "200",
        "max_parallel_workers_per_gather": "4",
    },
    "latency_leaning": {
        "shared_buffers": "65536",
        "effective_cache_size": "262144",
        "work_mem": "8192",
        "maintenance_work_mem": "131072",
        "checkpoint_timeout": "300",
        "checkpoint_completion_target": "0.95",
        "max_wal_size": "2048",
        "random_page_cost": "2",
        "effective_io_concurrency": "64",
        "default_statistics_target": "500",
        "max_parallel_workers_per_gather": "0",
    },
}


@dataclass(frozen=True)
class SaturationStep:
    action: str
    campaign_id: uuid.UUID
    ladder_id: uuid.UUID | None
    probe_id: uuid.UUID | None
    trial_id: uuid.UUID | None
    details: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_payload(path: Path) -> tuple[str, dict[str, Any]]:
    manifest = load_manifest(path)
    if manifest.stage != "sanity-and-saturation" or manifest.status != "ready":
        raise ValueError("Phase 1 saturation requires its deliberately ready manifest")
    phase = dict(manifest.payload.get("phase_1") or {})
    if phase.get("scales") != list(PHASE1_SCALES):
        raise ValueError("Phase 1 manifest scales differ from the implemented fixed plan")
    if phase.get("concurrency") != list(PHASE1_CONCURRENCY):
        raise ValueError("Phase 1 manifest concurrency differs from the implemented fixed plan")
    if phase.get("anchor_configurations") != list(PHASE1_ANCHOR_ORDER):
        raise ValueError("Phase 1 manifest anchor order differs from the implemented fixed plan")
    if phase.get("anchor_values") != PHASE1_ANCHORS:
        raise ValueError("Phase 1 manifest anchor values differ from the implemented fixed plan")
    if phase.get("warmup_seconds") != PHASE1_WARMUP_SECONDS:
        raise ValueError("Phase 1 manifest warm-up differs from the implemented fixed plan")
    if phase.get("measurement_seconds") != PHASE1_MEASUREMENT_SECONDS:
        raise ValueError("Phase 1 manifest measurement duration differs from the fixed plan")
    if phase.get("client_thread_cap") != PHASE1_CLIENT_THREAD_CAP:
        raise ValueError("Phase 1 manifest client thread cap differs from the fixed plan")
    return _sha256(path), manifest.payload


def create_phase1_plan(
    settings: Settings,
    manifest_path: Path = PHASE1_MANIFEST,
) -> uuid.UUID:
    manifest_sha256, manifest = _manifest_payload(manifest_path)
    campaign_id = create_campaign(
        settings,
        "v2-phase1-saturation-coarse-probes",
        "CALIBRATION",
        {"maximize": "throughput_tps", "minimize": "p99_ms", "diagnostic_only": True},
        {"failures": 0, "p99_slo_applied": False},
        failure_limit=9,
        campaign_settings={
            "protocol_id": "thesis-protocol-v2",
            "stage": "sanity-and-saturation-phase-1",
            "manifest_sha256": manifest_sha256,
            "candidate_restore_gate": "phase-1-exempt",
            "manifest": manifest,
        },
        actor="v2-saturation",
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        ladder_sequence = 0
        for scale in PHASE1_SCALES:
            for anchor_name in PHASE1_ANCHOR_ORDER:
                ladder_sequence += 1
                ladder_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"charmdb:{campaign_id}:phase1:{scale}:{anchor_name}",
                )
                cur.execute(
                    """INSERT INTO charm_control.experiment_saturation_phase1_ladders
                       (ladder_id,campaign_id,sequence,protocol_id,evidence_role,
                        manifest_sha256,scale,anchor_name,warmup_seconds,
                        measurement_seconds,client_thread_cap,requested_configuration,status)
                       VALUES (%s,%s,%s,'thesis-protocol-v2','CALIBRATION',%s,%s,%s,
                               %s,%s,%s,%s,'PLANNED')""",
                    (
                        ladder_id,
                        campaign_id,
                        ladder_sequence,
                        manifest_sha256,
                        scale,
                        anchor_name,
                        PHASE1_WARMUP_SECONDS,
                        PHASE1_MEASUREMENT_SECONDS,
                        PHASE1_CLIENT_THREAD_CAP,
                        Jsonb(PHASE1_ANCHORS[anchor_name]),
                    ),
                )
                for probe_sequence, concurrency in enumerate(PHASE1_CONCURRENCY, start=1):
                    probe_id = uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"charmdb:{ladder_id}:probe:{concurrency}",
                    )
                    cur.execute(
                        """INSERT INTO charm_control.experiment_saturation_phase1_probes
                           (probe_id,ladder_id,sequence,concurrency,client_threads,
                            random_seed,status)
                           VALUES (%s,%s,%s,%s,%s,%s,'PLANNED')""",
                        (
                            probe_id,
                            ladder_id,
                            probe_sequence,
                            concurrency,
                            min(PHASE1_CLIENT_THREAD_CAP, concurrency),
                            20260820 + ladder_sequence * 100 + probe_sequence,
                        ),
                    )
        conn.commit()
    return campaign_id


def _dataset_state(settings: Settings, scale: int) -> dict[str, Any]:
    relation_rows: dict[str, int] = {}
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        for relation in PGBENCH_RELATIONS:
            schema_name, relation_name = relation.split(".", 1)
            cur.execute(
                sql.SQL("SELECT count(*)::bigint AS count FROM {}.{}").format(
                    sql.Identifier(schema_name), sql.Identifier(relation_name)
                )
            )
            row = cur.fetchone()
            relation_rows[relation] = int(row["count"]) if row is not None else -1
        cur.execute("SELECT pg_database_size(current_database())::bigint AS bytes")
        size_row = cur.fetchone()
    expected = {
        "public.pgbench_accounts": scale * 100_000,
        "public.pgbench_branches": scale,
        "public.pgbench_history": 0,
        "public.pgbench_tellers": scale * 10,
    }
    if relation_rows != expected:
        raise RuntimeError(
            f"seeded Phase 1 dataset rows differ: expected={expected}, observed={relation_rows}"
        )
    return {
        "scale": scale,
        "relation_rows": relation_rows,
        "database_size_bytes": int(size_row["bytes"]) if size_row is not None else -1,
    }


def _prepare_ladder(settings: Settings, campaign_id: uuid.UUID, row: dict[str, Any]) -> None:
    ladder_id = uuid.UUID(str(row["ladder_id"]))
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        if row["status"] == "PLANNED":
            cur.execute(
                """UPDATE charm_control.experiment_saturation_phase1_ladders
                   SET status='PREPARING',started_at=clock_timestamp()
                   WHERE ladder_id=%s AND status='PLANNED'""",
                (ladder_id,),
            )
            conn.commit()
    _target_safety(settings, permitted_campaign_id=campaign_id)
    scale = int(row["scale"])
    seed_pgbench(settings, scale)
    standardize_logical_restore_state(settings)
    dataset = _dataset_state(settings, scale)
    requested = {str(key): str(value) for key, value in row["requested_configuration"].items()}
    application_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{ladder_id}:configuration")
    snapshot_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{ladder_id}:snapshot")
    application = apply_configuration(
        settings,
        requested,
        application_id=application_id,
        snapshot_id=snapshot_id,
    )
    resources = capture_and_validate_resources(settings)
    dataset["resource_snapshot_after_activation"] = resources
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_saturation_phase1_ladders
               SET status='READY',verified_configuration=%s,
                   configuration_application_id=%s,dataset_state=%s
               WHERE ladder_id=%s AND status='PREPARING'""",
            (Jsonb(application.verified), application_id, Jsonb(dataset), ladder_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Phase 1 ladder lost its PREPARING state")
        conn.commit()


def _create_probe_trial(
    settings: Settings,
    campaign_id: uuid.UUID,
    ladder: dict[str, Any],
    probe: dict[str, Any],
) -> uuid.UUID:
    ladder_id = uuid.UUID(str(ladder["ladder_id"]))
    probe_id = uuid.UUID(str(probe["probe_id"]))
    trial_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{probe_id}:trial")
    expected = {
        str(key): str(value) for key, value in ladder["verified_configuration"].items()
    }
    active = {
        str(row["name"]): str(row["setting"])
        for row in discover_knobs(settings, set(expected))
    }
    if active != expected:
        raise RuntimeError(f"Phase 1 ladder configuration drifted: {active} != {expected}")
    payload = {
        "warmup_seconds": int(ladder["warmup_seconds"]),
        "duration_seconds": int(ladder["measurement_seconds"]),
        "concurrency": int(probe["concurrency"]),
        "client_threads": int(probe["client_threads"]),
        "seed": int(probe["random_seed"]),
        "p99_slo_ms": 1_000_000_000.0,
        "expected_configuration": expected,
        "scale": int(ladder["scale"]),
        "anchor_name": str(ladder["anchor_name"]),
        "ladder_id": str(ladder_id),
        "probe_id": str(probe_id),
        "candidate_restore_exception": "phase-1-coarse-ladder",
        "diagnostic_only": True,
    }
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.trials
               (trial_id,campaign_id,state,benchmark_profile,fidelity,random_seed,
                requested_configuration,workflow_kind,workflow_payload,max_attempts,
                idempotency_key,protocol_id,evidence_role,evaluation_role,
                candidate_restore_required)
               VALUES (%s,%s,'CREATED','v2-saturation-phase1',2,%s,%s,%s,%s,3,%s,
                       'thesis-protocol-v2','CALIBRATION','SATURATION_COARSE',false)
               ON CONFLICT (campaign_id,idempotency_key) WHERE idempotency_key IS NOT NULL
               DO NOTHING""",
            (
                trial_id,
                campaign_id,
                int(probe["random_seed"]),
                Jsonb(expected),
                SATURATION_PHASE1_WORKFLOW,
                Jsonb(payload),
                f"phase1:{probe_id}",
            ),
        )
        cur.execute(
            """INSERT INTO charm_control.trial_transitions
               (trial_id,from_state,to_state,reason,details)
               VALUES (%s,NULL,'CREATED','Phase 1 coarse saturation probe created',%s)
               ON CONFLICT DO NOTHING""",
            (trial_id, Jsonb({"ladder_id": str(ladder_id), "probe_id": str(probe_id)})),
        )
        cur.execute(
            """UPDATE charm_control.experiment_saturation_phase1_probes
               SET status='CREATED',trial_id=%s
               WHERE probe_id=%s AND status='PLANNED'""",
            (trial_id, probe_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Phase 1 probe lost its PLANNED state")
        if ladder["status"] == "READY":
            cur.execute(
                """UPDATE charm_control.experiment_saturation_phase1_ladders
                   SET status='RUNNING' WHERE ladder_id=%s AND status='READY'""",
                (ladder_id,),
            )
        conn.commit()
    return trial_id


def _reconcile_probe(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any] | None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT p.probe_id,p.ladder_id,p.trial_id,t.state,t.completed_at
               FROM charm_control.experiment_saturation_phase1_probes p
               JOIN charm_control.experiment_saturation_phase1_ladders l USING(ladder_id)
               JOIN charm_control.trials t USING(trial_id)
               WHERE l.campaign_id=%s AND p.status='CREATED'
               ORDER BY l.sequence,p.sequence LIMIT 1""",
            (campaign_id,),
        )
        row = cur.fetchone()
        if row is None or row["completed_at"] is None:
            return dict(row) if row is not None else None
        passed = row["state"] == "COMPLETED"
        status = "COMPLETED" if passed else "FAILED"
        details = {} if passed else {"trial_state": str(row["state"])}
        cur.execute(
            """UPDATE charm_control.experiment_saturation_phase1_probes
               SET status=%s,failure_details=%s,completed_at=clock_timestamp()
               WHERE probe_id=%s AND status='CREATED'""",
            (status, Jsonb(details), row["probe_id"]),
        )
        conn.commit()
    return {**dict(row), "probe_status": status}


def _finalize_ladder_if_ready(
    settings: Settings,
    campaign_id: uuid.UUID,
    ladder: dict[str, Any],
) -> bool:
    ladder_id = uuid.UUID(str(ladder["ladder_id"]))
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT status,count(*) AS count
               FROM charm_control.experiment_saturation_phase1_probes
               WHERE ladder_id=%s GROUP BY status""",
            (ladder_id,),
        )
        counts = {str(row["status"]): int(row["count"]) for row in cur.fetchall()}
    if counts.get("FAILED", 0):
        target_status = "FAILED"
    elif counts.get("COMPLETED", 0) == len(PHASE1_CONCURRENCY):
        target_status = "COMPLETED"
    else:
        return False
    application_id = ladder.get("configuration_application_id")
    if application_id is not None:
        rollback_configuration(
            settings,
            uuid.UUID(str(application_id)),
            f"restore defaults after Phase 1 ladder {ladder_id}",
        )
    details = {} if target_status == "COMPLETED" else {"probe_status_counts": counts}
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_saturation_phase1_ladders
               SET status=%s,failure_details=%s,completed_at=clock_timestamp()
               WHERE ladder_id=%s AND status='RUNNING'""",
            (target_status, Jsonb(details), ladder_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Phase 1 ladder could not be finalized from RUNNING")
        conn.commit()
    if target_status == "FAILED":
        control_campaign(
            settings,
            campaign_id,
            "pause",
            f"Phase 1 ladder {ladder_id} failed",
            actor="v2-saturation",
        )
    return True


def run_phase1_next(
    settings: Settings,
    campaign_id: uuid.UUID,
    *,
    owner: str = "v2-saturation",
    lease_seconds: int = 600,
) -> SaturationStep:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM charm_control.campaigns WHERE campaign_id=%s",
            (campaign_id,),
        )
        campaign = cur.fetchone()
    if campaign is None:
        raise ValueError(f"unknown Phase 1 campaign {campaign_id}")
    if campaign["status"] != "RUNNING":
        raise ValueError(f"Phase 1 campaign must be RUNNING, not {campaign['status']}")

    reconciled = _reconcile_probe(settings, campaign_id)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_saturation_phase1_ladders
               WHERE campaign_id=%s AND status IN ('PREPARING','READY','RUNNING')
               ORDER BY sequence LIMIT 1""",
            (campaign_id,),
        )
        active_ladder_row = cur.fetchone()
    active_ladder = dict(active_ladder_row) if active_ladder_row is not None else None
    if (
        active_ladder is not None
        and active_ladder["status"] == "RUNNING"
        and _finalize_ladder_if_ready(settings, campaign_id, active_ladder)
    ):
        return SaturationStep(
            "ladder-finalized",
            campaign_id,
            uuid.UUID(str(active_ladder["ladder_id"])),
            None,
            None,
            {"status": "finalized"},
        )

    if reconciled is not None and reconciled.get("completed_at") is None:
        result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
        _reconcile_probe(settings, campaign_id)
        return SaturationStep(
            "probe-executed",
            campaign_id,
            uuid.UUID(str(reconciled["ladder_id"])),
            uuid.UUID(str(reconciled["probe_id"])),
            result.trial_id,
            {"trial_state": result.state},
        )

    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_saturation_phase1_ladders
               WHERE campaign_id=%s AND status IN ('PREPARING','READY','RUNNING')
               ORDER BY sequence LIMIT 1""",
            (campaign_id,),
        )
        active_ladder_row = cur.fetchone()
        if active_ladder_row is None:
            cur.execute(
                """SELECT * FROM charm_control.experiment_saturation_phase1_ladders
                   WHERE campaign_id=%s AND status='PLANNED' ORDER BY sequence LIMIT 1""",
                (campaign_id,),
            )
            planned_ladder_row = cur.fetchone()
        else:
            planned_ladder_row = None
    if active_ladder_row is None and planned_ladder_row is None:
        control_campaign(
            settings,
            campaign_id,
            "pause",
            "all Phase 1 saturation ladders completed",
            actor="v2-saturation",
        )
        return SaturationStep("campaign-complete", campaign_id, None, None, None, {})
    if active_ladder_row is None:
        if planned_ladder_row is None:
            raise RuntimeError("Phase 1 planned ladder disappeared")
        planned_ladder = dict(planned_ladder_row)
        _prepare_ladder(settings, campaign_id, planned_ladder)
        return SaturationStep(
            "ladder-prepared",
            campaign_id,
            uuid.UUID(str(planned_ladder["ladder_id"])),
            None,
            None,
            {"scale": planned_ladder["scale"], "anchor": planned_ladder["anchor_name"]},
        )

    active_ladder = dict(active_ladder_row)
    if active_ladder["status"] == "PREPARING":
        _prepare_ladder(settings, campaign_id, active_ladder)
        return SaturationStep(
            "ladder-prepared",
            campaign_id,
            uuid.UUID(str(active_ladder["ladder_id"])),
            None,
            None,
            {"scale": active_ladder["scale"], "anchor": active_ladder["anchor_name"]},
        )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_saturation_phase1_probes
               WHERE ladder_id=%s AND status='PLANNED' ORDER BY sequence LIMIT 1""",
            (active_ladder["ladder_id"],),
        )
        probe_row = cur.fetchone()
    if probe_row is None:
        if _finalize_ladder_if_ready(settings, campaign_id, active_ladder):
            return SaturationStep(
                "ladder-finalized",
                campaign_id,
                uuid.UUID(str(active_ladder["ladder_id"])),
                None,
                None,
                {},
            )
        raise RuntimeError("Phase 1 ladder has no runnable or finalizable probe")
    probe = dict(probe_row)
    trial_id = _create_probe_trial(settings, campaign_id, active_ladder, probe)
    result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
    _reconcile_probe(settings, campaign_id)
    return SaturationStep(
        "probe-executed",
        campaign_id,
        uuid.UUID(str(active_ladder["ladder_id"])),
        uuid.UUID(str(probe["probe_id"])),
        trial_id,
        {"trial_state": result.state, "concurrency": probe["concurrency"]},
    )


def phase1_history(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT ladder_id,sequence,scale,anchor_name,status,dataset_state,
                      verified_configuration,configuration_application_id,
                      failure_details,started_at,completed_at
               FROM charm_control.experiment_saturation_phase1_ladders
               WHERE campaign_id=%s ORDER BY sequence""",
            (campaign_id,),
        )
        ladders = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT l.scale,l.anchor_name,p.sequence,p.probe_id,p.concurrency,
                      p.client_threads,p.random_seed,p.status,p.trial_id,t.state,
                      t.objective_values,t.constraint_values,t.workflow_result,
                      p.failure_details,p.completed_at
               FROM charm_control.experiment_saturation_phase1_probes p
               JOIN charm_control.experiment_saturation_phase1_ladders l USING(ladder_id)
               LEFT JOIN charm_control.trials t USING(trial_id)
               WHERE l.campaign_id=%s ORDER BY l.sequence,p.sequence""",
            (campaign_id,),
        )
        probes = [dict(row) for row in cur.fetchall()]
    for collection in (ladders, probes):
        for row in collection:
            for key, value in tuple(row.items()):
                if isinstance(value, uuid.UUID):
                    row[key] = str(value)
    return {"campaign_id": str(campaign_id), "ladders": ladders, "probes": probes}


def detect_saturation_knee(points: list[dict[str, Any]]) -> int | None:
    ordered = sorted(points, key=lambda item: int(item["concurrency"]))
    for current, following in pairwise(ordered):
        current_tps = float(current["throughput_tps"])
        current_p99 = float(current["p99_ms"])
        tps_gain = (float(following["throughput_tps"]) - current_tps) / max(current_tps, 1e-9)
        p99_growth = (float(following["p99_ms"]) - current_p99) / max(current_p99, 1e-9)
        if tps_gain <= 0.10 and p99_growth >= 0.20:
            return int(current["concurrency"])
    return None


def _probe_analysis(row: dict[str, Any]) -> dict[str, Any] | None:
    objectives = dict(row.get("objective_values") or {})
    workflow_result = dict(row.get("workflow_result") or {})
    if not objectives or not workflow_result:
        return None
    telemetry = dict(workflow_result.get("runtime_telemetry") or {})
    samples = [dict(item) for item in telemetry.get("runtime_samples") or []]
    server_cpu = [float(item["container"]["cpu_percent"]) for item in samples]
    memory_usage = [int(item["container"]["memory_usage_bytes"]) for item in samples]
    client_capacity = float(telemetry.get("client_cpu_percent_of_thread_capacity") or 0.0)
    server_capacity = (sum(server_cpu) / len(server_cpu) / 400.0) if server_cpu else 0.0
    concurrency = int(row["concurrency"])
    return {
        "probe_id": str(row["probe_id"]),
        "trial_id": str(row["trial_id"]),
        "concurrency": concurrency,
        "client_threads": int(row["client_threads"]),
        "throughput_tps": float(objectives["throughput_tps"]),
        "p99_ms": float(objectives["p99_ms"]),
        "failures": int(workflow_result["failures"]),
        "client_cpu_percent_of_thread_capacity": client_capacity,
        "server_cpu_percent_mean": sum(server_cpu) / len(server_cpu) if server_cpu else None,
        "server_cpu_percent_max": max(server_cpu) if server_cpu else None,
        "server_capacity_fraction_mean": server_capacity,
        "server_memory_usage_bytes_max": max(memory_usage) if memory_usage else None,
        "runtime_sample_count": len(samples),
        "client_bottleneck_suspected": (
            concurrency >= 32 and client_capacity >= 85.0 and server_capacity < 0.85
        ),
    }


def phase1_analysis(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    history = phase1_history(settings, campaign_id)
    ladder_rows = {
        (int(row["scale"]), str(row["anchor_name"])): row for row in history["ladders"]
    }
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in history["probes"]:
        point = _probe_analysis(row)
        if point is not None:
            grouped.setdefault((int(row["scale"]), str(row["anchor_name"])), []).append(point)
    ladders: list[dict[str, Any]] = []
    for key in sorted(ladder_rows, key=lambda item: (item[0], PHASE1_ANCHOR_ORDER.index(item[1]))):
        row = ladder_rows[key]
        points = sorted(grouped.get(key, []), key=lambda item: int(item["concurrency"]))
        dataset = dict(row.get("dataset_state") or {})
        configuration = dict(row.get("verified_configuration") or {})
        shared_buffers_bytes = int(configuration.get("shared_buffers") or 0) * 8192
        database_size_bytes = int(dataset.get("database_size_bytes") or 0)
        ladders.append(
            {
                "ladder_id": str(row["ladder_id"]),
                "scale": key[0],
                "anchor_name": key[1],
                "status": row["status"],
                "database_size_bytes": database_size_bytes or None,
                "shared_buffers_bytes": shared_buffers_bytes or None,
                "database_to_shared_buffers_ratio": (
                    database_size_bytes / shared_buffers_bytes
                    if database_size_bytes and shared_buffers_bytes
                    else None
                ),
                "knee_concurrency": (
                    detect_saturation_knee(points)
                    if len(points) == len(PHASE1_CONCURRENCY)
                    else None
                ),
                "client_bottleneck_suspected": any(
                    bool(point["client_bottleneck_suspected"]) for point in points
                ),
                "points": points,
            }
        )
    completed_ladders = sum(row["status"] == "COMPLETED" for row in ladders)
    return {
        "campaign_id": str(campaign_id),
        "complete": completed_ladders == len(PHASE1_SCALES) * len(PHASE1_ANCHOR_ORDER),
        "completed_ladders": completed_ladders,
        "planned_ladders": len(PHASE1_SCALES) * len(PHASE1_ANCHOR_ORDER),
        "completed_probes": sum(len(row["points"]) for row in ladders),
        "planned_probes": (
            len(PHASE1_SCALES) * len(PHASE1_ANCHOR_ORDER) * len(PHASE1_CONCURRENCY)
        ),
        "client_bottleneck_suspected": any(
            bool(row["client_bottleneck_suspected"]) for row in ladders
        ),
        "ladders": ladders,
    }


def saturation_step_dict(step: SaturationStep) -> dict[str, Any]:
    return {
        "action": step.action,
        "campaign_id": str(step.campaign_id),
        "ladder_id": str(step.ladder_id) if step.ladder_id else None,
        "probe_id": str(step.probe_id) if step.probe_id else None,
        "trial_id": str(step.trial_id) if step.trial_id else None,
        "details": step.details,
    }
