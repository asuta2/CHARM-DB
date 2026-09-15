from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.controller import discover_knobs, validate_candidate
from charmdb.db import connect
from charmdb.protocol import load_manifest
from charmdb.worker import (
    control_campaign,
    create_campaign,
    create_tuned_benchmark_trial,
    run_once,
)

PHASE2_MANIFEST = Path("experiments/thesis/manifests/sanity-and-saturation.json")
PHASE2_CONFIGURATION_COUNT = 8
PHASE2_WARMUP_SECONDS = 120
PHASE2_MEASUREMENT_SECONDS = 600
PHASE2_SCALE = 500
PHASE2_CONCURRENCY = 32
PHASE2_CLIENT_THREADS = 4
PHASE2_MINIMUM_NONDOMINATED = 3
PHASE2_POLICY = "canonical-baseline-only-no-vacuum"


@dataclass(frozen=True)
class Phase2Step:
    action: str
    campaign_id: uuid.UUID
    configuration_id: uuid.UUID | None
    trial_id: uuid.UUID | None
    details: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def phase2_manifest_payload(path: Path = PHASE2_MANIFEST) -> tuple[str, dict[str, Any]]:
    manifest = load_manifest(path)
    if manifest.stage != "sanity-and-saturation" or manifest.status != "ready":
        raise ValueError("Phase 2 requires the ready saturation manifest")
    phase = dict(manifest.payload.get("phase_2") or {})
    expected = {
        "candidate_restore_gate": "required",
        "selected_restore_mechanism": "logical-restore",
        "selected_scale": PHASE2_SCALE,
        "selected_concurrency": PHASE2_CONCURRENCY,
        "client_threads": PHASE2_CLIENT_THREADS,
        "pgbench_maintenance_policy": PHASE2_POLICY,
        "representative_configuration_count": PHASE2_CONFIGURATION_COUNT,
        "warmup_seconds": PHASE2_WARMUP_SECONDS,
        "measurement_seconds": PHASE2_MEASUREMENT_SECONDS,
        "minimum_nondominated_points": PHASE2_MINIMUM_NONDOMINATED,
    }
    mismatches = {
        key: {"expected": value, "observed": phase.get(key)}
        for key, value in expected.items()
        if phase.get(key) != value
    }
    if mismatches:
        raise ValueError(f"Phase 2 manifest differs from the implemented plan: {mismatches}")
    if phase.get("status") not in {"ready-to-execute", "passed"}:
        raise ValueError("Phase 2 manifest is neither executable nor passed")
    if phase.get("restore_overhead_accepted") is not True:
        raise ValueError("Phase 2 restore overhead has not been accepted")
    if phase.get("blockers") != []:
        raise ValueError("ready Phase 2 manifest cannot retain execution blockers")
    configurations = phase.get("representative_configurations")
    if not isinstance(configurations, list) or len(configurations) != PHASE2_CONFIGURATION_COUNT:
        raise ValueError("Phase 2 requires exactly eight representative configurations")
    names: set[str] = set()
    knob_set: set[str] | None = None
    for item in configurations:
        if not isinstance(item, dict):
            raise ValueError("each Phase 2 configuration must be an object")
        name = item.get("name")
        values = item.get("values")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("Phase 2 configuration names must be non-empty and unique")
        if not isinstance(values, dict) or not values:
            raise ValueError(f"Phase 2 configuration {name!r} has no values")
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in values.items()
        ):
            raise ValueError(f"Phase 2 configuration {name!r} values must be strings")
        current_knobs = set(values)
        knob_set = current_knobs if knob_set is None else knob_set
        if current_knobs != knob_set:
            raise ValueError("all Phase 2 configurations must control the same knob set")
        names.add(name)
    return _sha256(path), manifest.payload


def create_phase2_plan(
    settings: Settings,
    preflight_id: uuid.UUID,
    manifest_path: Path = PHASE2_MANIFEST,
) -> uuid.UUID:
    readiness = phase2_readiness(settings, preflight_id, manifest_path)
    if readiness["ready"] is not True:
        raise ValueError("Phase 2 has already passed; refusing to create a duplicate campaign")
    manifest_sha256 = str(readiness["manifest_sha256"])
    manifest = dict(readiness["manifest"])
    phase = dict(manifest["phase_2"])
    campaign_id = create_campaign(
        settings,
        "v2-phase2-saturation-tradeoff-probe",
        "CALIBRATION",
        {"maximize": "throughput_tps", "minimize": "p99_ms"},
        {"failures": 0, "p99_slo_applied": False},
        failure_limit=1,
        campaign_settings={
            "protocol_id": "thesis-protocol-v2",
            "stage": "sanity-and-saturation-phase-2",
            "manifest_sha256": manifest_sha256,
            "preflight_id": str(preflight_id),
            "candidate_restore_baseline_id": phase["candidate_restore_baseline_id"],
            "candidate_restore_gate": "required",
            "restore_mechanism": phase["selected_restore_mechanism"],
            "restore_overhead_accepted": True,
            "pgbench_maintenance_policy": PHASE2_POLICY,
            "manifest": manifest,
        },
        actor="v2-saturation-phase2",
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        for sequence, configuration in enumerate(phase["representative_configurations"], start=1):
            configuration_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"charmdb:{campaign_id}:phase2:{sequence}:{configuration['name']}",
            )
            cur.execute(
                """INSERT INTO charm_control.experiment_saturation_phase2_configurations
                   (configuration_id,campaign_id,sequence,protocol_id,evidence_role,
                    manifest_sha256,configuration_name,requested_configuration,
                    random_seed,status)
                   VALUES (%s,%s,%s,'thesis-protocol-v2','CALIBRATION',%s,%s,%s,%s,'PLANNED')""",
                (
                    configuration_id,
                    campaign_id,
                    sequence,
                    manifest_sha256,
                    configuration["name"],
                    Jsonb(configuration["values"]),
                    20260830 + sequence,
                ),
            )
        conn.commit()
    return campaign_id


def phase2_readiness(
    settings: Settings,
    preflight_id: uuid.UUID,
    manifest_path: Path = PHASE2_MANIFEST,
) -> dict[str, Any]:
    manifest_sha256, manifest = phase2_manifest_payload(manifest_path)
    phase = dict(manifest["phase_2"])
    baseline_id = uuid.UUID(str(phase["candidate_restore_baseline_id"]))
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT preflight_id,restore_mechanism,approved
               FROM charm_control.experiment_candidate_dataset_baselines
               WHERE baseline_id=%s""",
            (baseline_id,),
        )
        baseline = cur.fetchone()
    if baseline is None or not baseline["approved"]:
        raise ValueError("Phase 2 requires its approved candidate baseline")
    if uuid.UUID(str(baseline["preflight_id"])) != preflight_id:
        raise ValueError("Phase 2 preflight does not own the selected candidate baseline")
    if baseline["restore_mechanism"] != phase["selected_restore_mechanism"]:
        raise ValueError("Phase 2 selected restore mechanism differs from its baseline")
    configurations = [
        {str(key): str(value) for key, value in configuration["values"].items()}
        for configuration in phase["representative_configurations"]
    ]
    metadata = discover_knobs(settings, set(configurations[0]))
    for configuration in configurations:
        validate_candidate(configuration, metadata)
    return {
        "ready": phase["status"] == "ready-to-execute",
        "phase2_status": phase["status"],
        "manifest_sha256": manifest_sha256,
        "manifest": manifest,
        "preflight_id": str(preflight_id),
        "baseline_id": str(baseline_id),
        "restore_mechanism": str(baseline["restore_mechanism"]),
        "configuration_count": len(configurations),
        "knob_count": len(configurations[0]),
        "pgbench_maintenance_policy": PHASE2_POLICY,
        "restore_inclusive_lower_bound_hours": phase["restore_inclusive_lower_bound_hours"],
    }


def _reconcile_configuration(
    settings: Settings, campaign_id: uuid.UUID
) -> dict[str, Any] | None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT c.configuration_id,c.trial_id,c.status,t.state,t.completed_at,
                      t.failure_type
               FROM charm_control.experiment_saturation_phase2_configurations c
               JOIN charm_control.trials t USING(trial_id)
               WHERE c.campaign_id=%s AND c.status='CREATED'
               ORDER BY c.sequence LIMIT 1""",
            (campaign_id,),
        )
        row = cur.fetchone()
        if row is None or row["completed_at"] is None:
            return dict(row) if row is not None else None
        passed = row["state"] == "COMPLETED"
        status = "COMPLETED" if passed else "FAILED"
        details = (
            {}
            if passed
            else {"trial_state": row["state"], "failure_type": row["failure_type"]}
        )
        cur.execute(
            """UPDATE charm_control.experiment_saturation_phase2_configurations
               SET status=%s,failure_details=%s,completed_at=clock_timestamp()
               WHERE configuration_id=%s AND status='CREATED'""",
            (status, Jsonb(details), row["configuration_id"]),
        )
        conn.commit()
    return {**dict(row), "configuration_status": status}


def run_phase2_next(
    settings: Settings,
    campaign_id: uuid.UUID,
    *,
    owner: str = "v2-saturation-phase2",
    lease_seconds: int = 600,
) -> Phase2Step:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status,settings FROM charm_control.campaigns WHERE campaign_id=%s",
            (campaign_id,),
        )
        campaign = cur.fetchone()
    if campaign is None:
        raise ValueError(f"unknown Phase 2 campaign {campaign_id}")
    if campaign["status"] != "RUNNING":
        raise ValueError(f"Phase 2 campaign must be RUNNING, not {campaign['status']}")
    campaign_settings = dict(campaign["settings"] or {})
    preflight_id = uuid.UUID(str(campaign_settings["preflight_id"]))

    reconciled = _reconcile_configuration(settings, campaign_id)
    if reconciled is not None and reconciled.get("configuration_status") == "FAILED":
        control_campaign(
            settings,
            campaign_id,
            "pause",
            f"Phase 2 trial {reconciled['trial_id']} failed",
            actor=owner,
        )
        return Phase2Step(
            "configuration-failed",
            campaign_id,
            uuid.UUID(str(reconciled["configuration_id"])),
            uuid.UUID(str(reconciled["trial_id"])),
            {"trial_state": reconciled["state"]},
        )
    if reconciled is not None and reconciled.get("completed_at") is None:
        result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
        finalized = _reconcile_configuration(settings, campaign_id)
        if finalized is not None and finalized.get("configuration_status") == "FAILED":
            control_campaign(
                settings,
                campaign_id,
                "pause",
                f"Phase 2 trial {finalized['trial_id']} failed",
                actor=owner,
            )
            return Phase2Step(
                "configuration-failed",
                campaign_id,
                uuid.UUID(str(finalized["configuration_id"])),
                uuid.UUID(str(finalized["trial_id"])),
                {"trial_state": finalized["state"]},
            )
        return Phase2Step(
            "configuration-executed",
            campaign_id,
            uuid.UUID(str(reconciled["configuration_id"])),
            result.trial_id,
            {"trial_state": result.state},
        )

    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_saturation_phase2_configurations
               WHERE campaign_id=%s AND status='PLANNED' ORDER BY sequence LIMIT 1""",
            (campaign_id,),
        )
        planned_row = cur.fetchone()
    if planned_row is None:
        control_campaign(
            settings,
            campaign_id,
            "stop",
            "all Phase 2 tradeoff configurations completed",
            actor=owner,
        )
        return Phase2Step("campaign-complete", campaign_id, None, None, {})

    planned = dict(planned_row)
    configuration_id = uuid.UUID(str(planned["configuration_id"]))
    trial_id = create_tuned_benchmark_trial(
        settings,
        campaign_id,
        preflight_id,
        dict(planned["requested_configuration"]),
        int(planned["random_seed"]),
        f"phase2:{configuration_id}",
        evidence_role="CALIBRATION",
        evaluation_role="SATURATION_TRADEOFF",
        warmup_seconds=PHASE2_WARMUP_SECONDS,
        duration_seconds=PHASE2_MEASUREMENT_SECONDS,
        concurrency=PHASE2_CONCURRENCY,
        client_threads=PHASE2_CLIENT_THREADS,
        restore_mechanism="logical-restore",
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_saturation_phase2_configurations
               SET status='CREATED',trial_id=%s
               WHERE configuration_id=%s AND status='PLANNED'""",
            (trial_id, configuration_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Phase 2 configuration lost its PLANNED state")
        conn.commit()
    result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
    finalized = _reconcile_configuration(settings, campaign_id)
    if finalized is not None and finalized.get("configuration_status") == "FAILED":
        control_campaign(
            settings,
            campaign_id,
            "pause",
            f"Phase 2 trial {finalized['trial_id']} failed",
            actor=owner,
        )
        return Phase2Step(
            "configuration-failed",
            campaign_id,
            uuid.UUID(str(finalized["configuration_id"])),
            uuid.UUID(str(finalized["trial_id"])),
            {"trial_state": finalized["state"]},
        )
    return Phase2Step(
        "configuration-executed",
        campaign_id,
        configuration_id,
        trial_id,
        {"trial_state": result.state, "configuration_name": planned["configuration_name"]},
    )


def phase2_history(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT c.configuration_id,c.sequence,c.configuration_name,
                      c.requested_configuration,c.random_seed,c.status,c.trial_id,
                      c.failure_details,c.created_at,c.completed_at,t.state,
                      t.objective_values,t.constraint_values,t.workflow_result
               FROM charm_control.experiment_saturation_phase2_configurations c
               LEFT JOIN charm_control.trials t USING(trial_id)
               WHERE c.campaign_id=%s ORDER BY c.sequence""",
            (campaign_id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        for key, value in tuple(row.items()):
            if isinstance(value, uuid.UUID):
                row[key] = str(value)
    return {"campaign_id": str(campaign_id), "configurations": rows}


def nondominated_phase2_points(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for candidate in points:
        dominated = any(
            other is not candidate
            and float(other["throughput_tps"]) >= float(candidate["throughput_tps"])
            and float(other["p99_ms"]) <= float(candidate["p99_ms"])
            and (
                float(other["throughput_tps"]) > float(candidate["throughput_tps"])
                or float(other["p99_ms"]) < float(candidate["p99_ms"])
            )
            for other in points
        )
        if not dominated:
            result.append(candidate)
    return sorted(result, key=lambda item: (-float(item["throughput_tps"]), float(item["p99_ms"])))


def phase2_analysis(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    history = phase2_history(settings, campaign_id)
    points: list[dict[str, Any]] = []
    for row in history["configurations"]:
        objectives = dict(row.get("objective_values") or {})
        constraints = dict(row.get("constraint_values") or {})
        if row["status"] != "COMPLETED" or not objectives:
            continue
        points.append(
            {
                "configuration_id": row["configuration_id"],
                "trial_id": row["trial_id"],
                "configuration_name": row["configuration_name"],
                "throughput_tps": float(objectives["throughput_tps"]),
                "p99_ms": float(objectives["p99_ms"]),
                "failures": int(constraints.get("failures", 0)),
            }
        )
    nondominated = nondominated_phase2_points(points)
    complete = len(points) == PHASE2_CONFIGURATION_COUNT
    return {
        "campaign_id": str(campaign_id),
        "complete": complete,
        "completed_configurations": len(points),
        "planned_configurations": PHASE2_CONFIGURATION_COUNT,
        "minimum_nondominated_points": PHASE2_MINIMUM_NONDOMINATED,
        "nondominated_count": len(nondominated),
        "passed": complete and len(nondominated) >= PHASE2_MINIMUM_NONDOMINATED,
        "points": points,
        "nondominated": nondominated,
    }


def phase2_step_dict(step: Phase2Step) -> dict[str, Any]:
    return {
        "action": step.action,
        "campaign_id": str(step.campaign_id),
        "configuration_id": str(step.configuration_id) if step.configuration_id else None,
        "trial_id": str(step.trial_id) if step.trial_id else None,
        "details": step.details,
    }
