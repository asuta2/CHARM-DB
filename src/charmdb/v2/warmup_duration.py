from __future__ import annotations

import hashlib
import json
import random
import statistics
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.controller import discover_knobs, validate_candidate
from charmdb.db import connect
from charmdb.manifest_preflight import _target_safety
from charmdb.v2.protocol import load_manifest
from charmdb.v2.saturation_phase2 import PHASE2_MANIFEST, phase2_manifest_payload
from charmdb.v2.window_analysis import (
    analyze_measurement_marker,
    analyze_rolling_measurement,
    compare_duration_windows,
)
from charmdb.worker import (
    control_campaign,
    create_campaign,
    create_v2_tuned_benchmark_trial,
    run_once,
)

PILOT_MANIFEST = Path("v2/config/warmup-and-f3-duration-pilot.json")
PILOT_BASELINE_ID = uuid.UUID("35459275-c9bf-544f-be54-4e3537464c78")
PILOT_PHASE2_CAMPAIGN_ID = uuid.UUID("9f8c1124-137d-4d65-9545-2b445b3cc3fc")
PILOT_CONCURRENCY = 32
PILOT_CLIENT_THREADS = 4
PILOT_POLICY = "canonical-baseline-only-no-vacuum"


@dataclass(frozen=True)
class PilotStep:
    action: str
    campaign_id: uuid.UUID
    pilot_id: uuid.UUID
    run_id: uuid.UUID | None
    trial_id: uuid.UUID | None
    details: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def pilot_manifest_payload(path: Path = PILOT_MANIFEST) -> tuple[str, dict[str, Any]]:
    manifest = load_manifest(path)
    if manifest.stage != "warmup-and-f3-duration-pilot":
        raise ValueError("warm-up/duration pilot requires its named manifest stage")
    if manifest.status not in {"draft", "ready"}:
        raise ValueError("warm-up/duration pilot manifest must be draft or ready")
    payload = manifest.payload
    if payload.get("evidence_role") != "CALIBRATION":
        raise ValueError("warm-up/duration pilot must be CALIBRATION evidence")
    if payload.get("operator_triggered") is not True:
        raise ValueError("warm-up/duration pilot must remain operator-triggered")
    if payload.get("candidate_restore_gate") != "required":
        raise ValueError("warm-up/duration pilot requires candidate-level restore")
    prerequisites = dict(payload.get("prerequisites") or {})
    expected_prerequisites = {
        "workload_tradeoff_gate_passed": True,
        "workload_tradeoff_campaign_id": str(PILOT_PHASE2_CAMPAIGN_ID),
        "restore_mechanism_selected": True,
        "restore_mechanism": "logical-restore",
        "candidate_restore_baseline_id": str(PILOT_BASELINE_ID),
    }
    mismatches = {
        key: {"expected": expected, "observed": prerequisites.get(key)}
        for key, expected in expected_prerequisites.items()
        if prerequisites.get(key) != expected
    }
    if mismatches:
        raise ValueError(f"pilot prerequisites differ from the frozen design: {mismatches}")

    warmup = dict(payload.get("warmup_calibration") or {})
    duration = dict(payload.get("duration_pilot") or {})
    thresholds = dict(payload.get("acceptance_thresholds") or {})
    expected_warmup = {
        "warmup_seconds": 0,
        "measurement_seconds": 1200,
        "rolling_window_seconds": 60,
    }
    expected_duration = {
        "randomized_blocks": 3,
        "long_runs": 18,
        "long_measurement_seconds": 600,
        "standalone_short_runs": 3,
        "short_measurement_seconds": 300,
        "rolling_windows_required": True,
        "standalone_short_configuration": "postgresql_default",
    }
    for section_name, section, expected_values in (
        ("warmup_calibration", warmup, expected_warmup),
        ("duration_pilot", duration, expected_duration),
    ):
        differences = {
            key: {"expected": expected, "observed": section.get(key)}
            for key, expected in expected_values.items()
            if section.get(key) != expected
        }
        if differences:
            raise ValueError(f"{section_name} differs from the implemented design: {differences}")
    warmup_names = warmup.get("representative_configurations")
    duration_names = duration.get("representative_configurations")
    if not isinstance(warmup_names, list) or len(warmup_names) != 4:
        raise ValueError("warm-up calibration requires exactly four representatives")
    if not isinstance(duration_names, list) or len(duration_names) != 6:
        raise ValueError("duration pilot requires exactly six representatives")
    if len(set(map(str, warmup_names))) != 4 or len(set(map(str, duration_names))) != 6:
        raise ValueError("pilot representative configuration names must be unique")
    if not isinstance(duration.get("schedule_seed"), int):
        raise ValueError("duration pilot requires a pre-registered integer schedule seed")
    expected_thresholds = {
        "median_tps_relative": 0.05,
        "median_p99_ms_absolute": 1.0,
        "rank_correlation": 0.9,
        "pareto_membership_agreement": 0.8,
        "standalone_tps_relative": 0.05,
        "standalone_p99_ms_absolute": 1.0,
    }
    if any(thresholds.get(key) != value for key, value in expected_thresholds.items()):
        raise ValueError("pilot acceptance thresholds differ from the pre-registered design")
    return _sha256(path), payload


def _phase2_configurations(path: Path = PHASE2_MANIFEST) -> tuple[str, dict[str, dict[str, str]]]:
    digest, payload = phase2_manifest_payload(path)
    configurations = {
        str(item["name"]): {str(key): str(value) for key, value in item["values"].items()}
        for item in payload["phase_2"]["representative_configurations"]
    }
    return digest, configurations


def duration_schedule(
    configuration_names: list[str],
    *,
    blocks: int,
    standalone_configuration: str,
    schedule_seed: int,
) -> list[dict[str, Any]]:
    if len(configuration_names) != 6 or len(set(configuration_names)) != 6:
        raise ValueError("duration schedule requires six unique configurations")
    if standalone_configuration not in configuration_names:
        raise ValueError("standalone configuration must be one of the long-run configurations")
    if blocks != 3:
        raise ValueError("duration schedule requires exactly three blocks")
    schedule: list[dict[str, Any]] = []
    for block in range(1, blocks + 1):
        rows = [
            {"subphase": "DURATION_LONG", "configuration_name": name, "block": block}
            for name in configuration_names
        ]
        rows.append(
            {
                "subphase": "DURATION_SHORT",
                "configuration_name": standalone_configuration,
                "block": block,
            }
        )
        random.Random(schedule_seed + block).shuffle(rows)
        schedule.extend(rows)
    return schedule


def pilot_readiness(
    settings: Settings,
    preflight_id: uuid.UUID,
    manifest_path: Path = PILOT_MANIFEST,
    phase2_manifest_path: Path = PHASE2_MANIFEST,
) -> dict[str, Any]:
    manifest_sha256, manifest = pilot_manifest_payload(manifest_path)
    phase2_sha256, configurations = _phase2_configurations(phase2_manifest_path)
    names = list(manifest["warmup_calibration"]["representative_configurations"])
    names += list(manifest["duration_pilot"]["representative_configurations"])
    missing = sorted(set(map(str, names)) - set(configurations))
    if missing:
        raise ValueError(
            f"pilot configurations are absent from the frozen Phase 2 design: {missing}"
        )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT preflight_id,restore_mechanism,approved
               FROM charm_control.experiment_candidate_dataset_baselines
               WHERE baseline_id=%s""",
            (PILOT_BASELINE_ID,),
        )
        baseline = cur.fetchone()
        cur.execute(
            """SELECT count(*) AS completed
               FROM charm_control.experiment_saturation_phase2_configurations
               WHERE campaign_id=%s AND status='COMPLETED'""",
            (PILOT_PHASE2_CAMPAIGN_ID,),
        )
        phase2 = cur.fetchone()
    if baseline is None or baseline["approved"] is not True:
        raise ValueError("pilot requires the approved scale-500 candidate baseline")
    if uuid.UUID(str(baseline["preflight_id"])) != preflight_id:
        raise ValueError("pilot preflight does not own the selected candidate baseline")
    if baseline["restore_mechanism"] != "logical-restore":
        raise ValueError("pilot requires the selected logical restore baseline")
    if phase2 is None or int(phase2["completed"]) != 8:
        raise ValueError("pilot requires all eight frozen Phase 2 configurations to complete")
    _target_safety(settings)
    metadata = discover_knobs(settings, set(next(iter(configurations.values()))))
    for configuration in configurations.values():
        validate_candidate(configuration, metadata)
    return {
        "ready": (
            manifest["status"] == "ready"
            and manifest.get("restore_inclusive_runtime_accepted") is True
        ),
        "manifest_status": manifest["status"],
        "restore_inclusive_runtime_accepted": manifest.get(
            "restore_inclusive_runtime_accepted", False
        ),
        "manifest_sha256": manifest_sha256,
        "phase2_manifest_sha256": phase2_sha256,
        "preflight_id": str(preflight_id),
        "baseline_id": str(PILOT_BASELINE_ID),
        "warmup_runs": 4,
        "duration_runs": 21,
        "combined_restore_inclusive_lower_bound_hours": manifest[
            "combined_restore_inclusive_lower_bound_hours"
        ],
    }


def create_pilot_plan(
    settings: Settings,
    preflight_id: uuid.UUID,
    manifest_path: Path = PILOT_MANIFEST,
    phase2_manifest_path: Path = PHASE2_MANIFEST,
) -> uuid.UUID:
    readiness = pilot_readiness(settings, preflight_id, manifest_path, phase2_manifest_path)
    if readiness["ready"] is not True:
        raise ValueError(
            "pilot execution is blocked until the manifest is ready and its runtime is accepted"
        )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT p.campaign_id,p.status,c.status AS campaign_status
               FROM charm_control.experiment_warmup_duration_pilots p
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE p.status<>'FAILED'
               ORDER BY p.created_at DESC LIMIT 1"""
        )
        existing = cur.fetchone()
    if existing is not None:
        raise ValueError(
            "a non-failed warm-up/duration pilot already exists: "
            f"campaign={existing['campaign_id']} pilot_status={existing['status']} "
            f"campaign_status={existing['campaign_status']}"
        )
    _, manifest = pilot_manifest_payload(manifest_path)
    _, configurations = _phase2_configurations(phase2_manifest_path)
    campaign_id = create_campaign(
        settings,
        "v2-warmup-and-f3-duration-pilot",
        "CALIBRATION",
        {"maximize": "throughput_tps", "minimize": "p99_ms"},
        {"failures": 0, "p99_slo_applied": False},
        failure_limit=1,
        campaign_settings={
            "protocol_id": "thesis-protocol-v2",
            "stage": "warmup-and-f3-duration-pilot",
            "preflight_id": str(preflight_id),
            "candidate_restore_baseline_id": str(PILOT_BASELINE_ID),
            "candidate_restore_gate": "required",
            "restore_mechanism": "logical-restore",
            "pgbench_maintenance_policy": PILOT_POLICY,
            "manifest_sha256": readiness["manifest_sha256"],
            "phase2_manifest_sha256": readiness["phase2_manifest_sha256"],
            "manifest": manifest,
        },
        actor="v2-warmup-duration",
    )
    pilot_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{campaign_id}:warmup-duration-pilot")
    warmup = manifest["warmup_calibration"]
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_warmup_duration_pilots
               (pilot_id,campaign_id,protocol_id,evidence_role,manifest_sha256,
                phase2_manifest_sha256,preflight_id,baseline_id,status)
               VALUES (%s,%s,'thesis-protocol-v2','CALIBRATION',%s,%s,%s,%s,
                       'WARMUP_PLANNED')""",
            (
                pilot_id,
                campaign_id,
                readiness["manifest_sha256"],
                readiness["phase2_manifest_sha256"],
                preflight_id,
                PILOT_BASELINE_ID,
            ),
        )
        for sequence, name in enumerate(warmup["representative_configurations"], start=1):
            run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{pilot_id}:warmup:{sequence}:{name}")
            cur.execute(
                """INSERT INTO charm_control.experiment_warmup_duration_runs
                   (run_id,pilot_id,campaign_id,sequence,subphase,configuration_name,
                    requested_configuration,warmup_seconds,measurement_seconds,random_seed,status)
                   VALUES (%s,%s,%s,%s,'WARMUP',%s,%s,%s,%s,%s,'PLANNED')""",
                (
                    run_id,
                    pilot_id,
                    campaign_id,
                    sequence,
                    name,
                    Jsonb(configurations[str(name)]),
                    int(warmup["warmup_seconds"]),
                    int(warmup["measurement_seconds"]),
                    20260900 + sequence,
                ),
            )
        conn.commit()
    return campaign_id


def _pilot(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT p.*,c.status AS campaign_status,c.settings AS campaign_settings
               FROM charm_control.experiment_warmup_duration_pilots p
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE p.campaign_id=%s""",
            (campaign_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown warm-up/duration pilot campaign {campaign_id}")
    return dict(row)


def _reconcile_run(settings: Settings, pilot_id: uuid.UUID) -> dict[str, Any] | None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.run_id,r.trial_id,r.subphase,r.status,t.state,t.completed_at,
                      t.failure_type
               FROM charm_control.experiment_warmup_duration_runs r
               JOIN charm_control.trials t USING(trial_id)
               WHERE r.pilot_id=%s AND r.status='CREATED'
               ORDER BY r.sequence LIMIT 1""",
            (pilot_id,),
        )
        row = cur.fetchone()
        if row is None or row["completed_at"] is None:
            return dict(row) if row is not None else None
        passed = row["state"] == "COMPLETED"
        status = "COMPLETED" if passed else "FAILED"
        details = (
            {} if passed else {"trial_state": row["state"], "failure_type": row["failure_type"]}
        )
        cur.execute(
            """UPDATE charm_control.experiment_warmup_duration_runs
               SET status=%s,failure_details=%s,completed_at=clock_timestamp()
               WHERE run_id=%s AND status='CREATED'""",
            (status, Jsonb(details), row["run_id"]),
        )
        conn.commit()
    return {**dict(row), "run_status": status}


def _pause_failed_run(
    settings: Settings,
    campaign_id: uuid.UUID,
    pilot_id: uuid.UUID,
    reconciled: dict[str, Any],
    owner: str,
) -> PilotStep:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_warmup_duration_pilots
               SET status='FAILED'
               WHERE pilot_id=%s AND status NOT IN ('COMPLETED','FAILED')""",
            (pilot_id,),
        )
        cur.execute(
            "SELECT status FROM charm_control.campaigns WHERE campaign_id=%s",
            (campaign_id,),
        )
        campaign = cur.fetchone()
        conn.commit()
    campaign_status = str(campaign["status"]) if campaign is not None else "UNKNOWN"
    if campaign_status == "RUNNING":
        control_campaign(
            settings,
            campaign_id,
            "pause",
            f"pilot trial {reconciled['trial_id']} failed",
            actor=owner,
        )
        campaign_status = "PAUSED"
    return PilotStep(
        "run-failed",
        campaign_id,
        pilot_id,
        uuid.UUID(str(reconciled["run_id"])),
        uuid.UUID(str(reconciled["trial_id"])),
        {
            "trial_state": reconciled["state"],
            "subphase": reconciled["subphase"],
            "campaign_status": campaign_status,
        },
    )


def run_pilot_next(
    settings: Settings,
    campaign_id: uuid.UUID,
    *,
    owner: str = "v2-warmup-duration",
    lease_seconds: int = 600,
) -> PilotStep:
    pilot = _pilot(settings, campaign_id)
    pilot_id = uuid.UUID(str(pilot["pilot_id"]))
    reconciled = _reconcile_run(settings, pilot_id)
    if reconciled is not None and reconciled.get("run_status") == "FAILED":
        return _pause_failed_run(settings, campaign_id, pilot_id, reconciled, owner)
    if pilot["campaign_status"] != "RUNNING":
        raise ValueError(f"pilot campaign must be RUNNING, not {pilot['campaign_status']}")
    if pilot["status"] not in {
        "WARMUP_PLANNED",
        "WARMUP_RUNNING",
        "WARMUP_FROZEN",
        "DURATION_RUNNING",
    }:
        raise ValueError(f"pilot cannot run from status {pilot['status']}")
    if pilot["status"] == "WARMUP_PLANNED":
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_warmup_duration_pilots
                   SET status='WARMUP_RUNNING' WHERE pilot_id=%s AND status='WARMUP_PLANNED'""",
                (pilot_id,),
            )
            conn.commit()
        pilot["status"] = "WARMUP_RUNNING"
    elif pilot["status"] == "WARMUP_FROZEN":
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_warmup_duration_pilots
                   SET status='DURATION_RUNNING' WHERE pilot_id=%s AND status='WARMUP_FROZEN'""",
                (pilot_id,),
            )
            conn.commit()
        pilot["status"] = "DURATION_RUNNING"

    if reconciled is not None and reconciled.get("completed_at") is None:
        result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
        finalized = _reconcile_run(settings, pilot_id)
        if finalized is not None and finalized.get("run_status") == "FAILED":
            return _pause_failed_run(settings, campaign_id, pilot_id, finalized, owner)
        return PilotStep(
            "run-executed",
            campaign_id,
            pilot_id,
            uuid.UUID(str(reconciled["run_id"])),
            result.trial_id,
            {"trial_state": result.state, "subphase": reconciled["subphase"]},
        )

    active_subphases = (
        ("WARMUP",) if pilot["status"] == "WARMUP_RUNNING" else ("DURATION_LONG", "DURATION_SHORT")
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_warmup_duration_runs
               WHERE pilot_id=%s AND subphase=ANY(%s) AND status='PLANNED'
               ORDER BY sequence LIMIT 1""",
            (pilot_id, list(active_subphases)),
        )
        planned_row = cur.fetchone()
    if planned_row is None:
        if pilot["status"] == "WARMUP_RUNNING":
            next_status = "WARMUP_COMPLETE"
            reason = "all four warm-up calibration runs completed; warm-up decision required"
            action = "warmup-complete"
        else:
            next_status = "COMPLETED"
            reason = "all 18 long and three standalone-short duration runs completed"
            action = "duration-complete"
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_warmup_duration_pilots
                   SET status=%s,completed_at=CASE WHEN %s='COMPLETED'
                       THEN clock_timestamp() ELSE completed_at END
                   WHERE pilot_id=%s""",
                (next_status, next_status, pilot_id),
            )
            conn.commit()
        control_campaign(settings, campaign_id, "pause", reason, actor=owner)
        return PilotStep(action, campaign_id, pilot_id, None, None, {})

    planned = dict(planned_row)
    run_id = uuid.UUID(str(planned["run_id"]))
    trial_id = create_v2_tuned_benchmark_trial(
        settings,
        campaign_id,
        uuid.UUID(str(pilot["preflight_id"])),
        {str(key): str(value) for key, value in dict(planned["requested_configuration"]).items()},
        int(planned["random_seed"]),
        f"warmup-duration:{run_id}",
        evidence_role="CALIBRATION",
        evaluation_role=str(planned["subphase"]),
        warmup_seconds=int(planned["warmup_seconds"]),
        duration_seconds=int(planned["measurement_seconds"]),
        concurrency=PILOT_CONCURRENCY,
        client_threads=PILOT_CLIENT_THREADS,
        restore_mechanism="logical-restore",
        benchmark_profile="v2-warmup-duration-pilot",
        runtime_samples_required=planned["subphase"] == "WARMUP",
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_warmup_duration_runs
               SET status='CREATED',trial_id=%s WHERE run_id=%s AND status='PLANNED'""",
            (trial_id, run_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("pilot run lost its PLANNED state")
        conn.commit()
    result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
    finalized = _reconcile_run(settings, pilot_id)
    if finalized is not None and finalized.get("run_status") == "FAILED":
        return _pause_failed_run(settings, campaign_id, pilot_id, finalized, owner)
    return PilotStep(
        "run-executed",
        campaign_id,
        pilot_id,
        run_id,
        trial_id,
        {
            "trial_state": result.state,
            "subphase": planned["subphase"],
            "configuration_name": planned["configuration_name"],
            "block": planned["block"],
        },
    )


def pilot_history(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    pilot = _pilot(settings, campaign_id)
    pilot_id = uuid.UUID(str(pilot["pilot_id"]))
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.*,t.state,t.objective_values,t.constraint_values,t.workflow_result,
                      t.started_at AS trial_started_at,t.completed_at AS trial_completed_at,
                      cr.duration_seconds AS restore_seconds
               FROM charm_control.experiment_warmup_duration_runs r
               LEFT JOIN charm_control.trials t USING(trial_id)
               LEFT JOIN charm_control.experiment_candidate_dataset_restores cr
                 ON cr.restore_id=t.candidate_dataset_restore_id
               WHERE r.pilot_id=%s ORDER BY r.sequence""",
            (pilot_id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
        trial_ids = [row["trial_id"] for row in rows if row["trial_id"] is not None]
        actions: dict[str, dict[str, float]] = {}
        if trial_ids:
            cur.execute(
                """SELECT trial_id,state,
                          extract(epoch FROM (completed_at-started_at)) AS seconds
                   FROM charm_control.trial_action_executions
                   WHERE trial_id=ANY(%s) AND status='COMPLETED'
                   ORDER BY started_at""",
                (trial_ids,),
            )
            for action in cur.fetchall():
                key = str(action["trial_id"])
                actions.setdefault(key, {})[str(action["state"])] = float(action["seconds"])
    for row in rows:
        for key, value in tuple(row.items()):
            if isinstance(value, uuid.UUID):
                row[key] = str(value)
        row["action_seconds"] = actions.get(str(row.get("trial_id")), {})
        if row.get("trial_started_at") is not None and row.get("trial_completed_at") is not None:
            row["total_seconds"] = (
                row["trial_completed_at"] - row["trial_started_at"]
            ).total_seconds()
    pilot_payload = {
        key: str(value) if isinstance(value, uuid.UUID) else value
        for key, value in pilot.items()
        if key not in {"campaign_settings"}
    }
    return {"pilot": pilot_payload, "runs": rows}


def _measurement_marker(settings: Settings, row: dict[str, Any]) -> Path:
    workflow_result = dict(row.get("workflow_result") or {})
    relative = workflow_result.get("measurement_marker")
    if not isinstance(relative, str) or not relative:
        # Durable benchmark observations written before the marker was copied into
        # trials.workflow_result still retain it in their immutable trial artifact.
        # Use that artifact as the compatibility source so completed evidence does
        # not need to be mutated in the control database.
        campaign_id = uuid.UUID(str(row["campaign_id"]))
        trial_id = uuid.UUID(str(row["trial_id"]))
        trial_artifact = (
            settings.artifact_dir / "raw" / str(campaign_id) / str(trial_id) / "trial.json"
        )
        if trial_artifact.is_file():
            payload = json.loads(trial_artifact.read_text(encoding="utf-8"))
            relative = payload.get("measurement_marker")
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"completed pilot run {row['run_id']} has no measurement marker")
    artifact_root = settings.artifact_dir.resolve()
    marker = (artifact_root / Path(relative.replace("\\", "/"))).resolve()
    try:
        marker.relative_to(artifact_root)
    except ValueError as exc:
        raise ValueError("measurement marker escapes the artifact directory") from exc
    if not marker.is_file():
        raise ValueError(f"measurement marker does not exist: {marker}")
    return marker


def analyze_warmup(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    history = pilot_history(settings, campaign_id)
    pilot = history["pilot"]
    if pilot["status"] not in {"WARMUP_COMPLETE", "WARMUP_FROZEN", "DURATION_RUNNING", "COMPLETED"}:
        raise ValueError("warm-up analysis requires all four warm-up runs to complete")
    warmup_rows = [row for row in history["runs"] if row["subphase"] == "WARMUP"]
    if len(warmup_rows) != 4 or any(row["status"] != "COMPLETED" for row in warmup_rows):
        raise ValueError("warm-up analysis requires four completed warm-up runs")
    analyses = []
    for row in warmup_rows:
        analysis = analyze_rolling_measurement(_measurement_marker(settings, row))
        analyses.append(
            {
                "run_id": row["run_id"],
                "trial_id": row["trial_id"],
                "configuration_name": row["configuration_name"],
                "restore_seconds": row.get("restore_seconds"),
                "total_seconds": row.get("total_seconds"),
                "action_seconds": row.get("action_seconds", {}),
                **analysis,
            }
        )
    payload = {
        "campaign_id": str(campaign_id),
        "pilot_id": str(pilot["pilot_id"]),
        "complete": True,
        "decision_required": True,
        "selection_rule": (
            "choose the shortest common warm-up after which TPS, p99, PostgreSQL activity, "
            "CPU, memory, and I/O are reproducibly stable across all four representatives"
        ),
        "runs": analyses,
    }
    payload["analysis_sha256"] = _json_sha256(payload)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_warmup_duration_pilots
               SET warmup_analysis=%s,warmup_analysis_sha256=%s
               WHERE campaign_id=%s AND status='WARMUP_COMPLETE'""",
            (Jsonb(payload), payload["analysis_sha256"], campaign_id),
        )
        conn.commit()
    return payload


def freeze_warmup(
    settings: Settings,
    campaign_id: uuid.UUID,
    selected_warmup_seconds: int,
    analysis_sha256: str,
    reason: str,
) -> dict[str, Any]:
    if not 0 <= selected_warmup_seconds <= 1200:
        raise ValueError("selected warm-up must be between zero and 1200 seconds")
    if len(analysis_sha256) != 64:
        raise ValueError("warm-up analysis SHA-256 must contain 64 characters")
    if not reason.strip():
        raise ValueError("warm-up freeze requires an evidence-backed reason")
    pilot = _pilot(settings, campaign_id)
    if pilot["campaign_status"] != "PAUSED" or pilot["status"] != "WARMUP_COMPLETE":
        raise ValueError("warm-up can freeze only after the campaign pauses at WARMUP_COMPLETE")
    if pilot["warmup_analysis"] is None or pilot["warmup_analysis_sha256"] != analysis_sha256:
        raise ValueError("warm-up freeze analysis hash does not match persisted analysis")
    _, manifest = pilot_manifest_payload()
    _, configurations = _phase2_configurations()
    duration = manifest["duration_pilot"]
    schedule = duration_schedule(
        [str(name) for name in duration["representative_configurations"]],
        blocks=int(duration["randomized_blocks"]),
        standalone_configuration=str(duration["standalone_short_configuration"]),
        schedule_seed=int(duration["schedule_seed"]),
    )
    pilot_id = uuid.UUID(str(pilot["pilot_id"]))
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        for sequence, item in enumerate(schedule, start=5):
            name = str(item["configuration_name"])
            subphase = str(item["subphase"])
            measurement_seconds = (
                int(duration["long_measurement_seconds"])
                if subphase == "DURATION_LONG"
                else int(duration["short_measurement_seconds"])
            )
            run_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"charmdb:{pilot_id}:duration:{sequence}:{item['block']}:{subphase}:{name}",
            )
            cur.execute(
                """INSERT INTO charm_control.experiment_warmup_duration_runs
                   (run_id,pilot_id,campaign_id,sequence,subphase,block,configuration_name,
                    requested_configuration,warmup_seconds,measurement_seconds,random_seed,status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PLANNED')""",
                (
                    run_id,
                    pilot_id,
                    campaign_id,
                    sequence,
                    subphase,
                    item["block"],
                    name,
                    Jsonb(configurations[name]),
                    selected_warmup_seconds,
                    measurement_seconds,
                    20261000 + int(item["block"]) * 100 + sequence,
                ),
            )
        cur.execute(
            """UPDATE charm_control.experiment_warmup_duration_pilots
               SET status='WARMUP_FROZEN',selected_warmup_seconds=%s,
                   warmup_decision_reason=%s,warmup_frozen_at=clock_timestamp()
               WHERE pilot_id=%s AND status='WARMUP_COMPLETE'""",
            (selected_warmup_seconds, reason.strip(), pilot_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("pilot lost its WARMUP_COMPLETE state")
        conn.commit()
    return {
        "campaign_id": str(campaign_id),
        "pilot_id": str(pilot_id),
        "status": "WARMUP_FROZEN",
        "selected_warmup_seconds": selected_warmup_seconds,
        "analysis_sha256": analysis_sha256,
        "duration_runs_planned": len(schedule),
        "next_action": "explicitly resume the campaign before duration execution",
    }


def analyze_duration(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    history = pilot_history(settings, campaign_id)
    pilot = history["pilot"]
    if pilot["status"] != "COMPLETED":
        raise ValueError("duration analysis requires the completed 18+3 pilot")
    long_rows = [row for row in history["runs"] if row["subphase"] == "DURATION_LONG"]
    short_rows = [row for row in history["runs"] if row["subphase"] == "DURATION_SHORT"]
    if len(long_rows) != 18 or len(short_rows) != 3:
        raise ValueError("duration analysis requires exactly 18 long and three short runs")
    if any(row["status"] != "COMPLETED" for row in [*long_rows, *short_rows]):
        raise ValueError("duration analysis requires every duration run to complete")
    analyzed_long = []
    for row in long_rows:
        windows = analyze_measurement_marker(_measurement_marker(settings, row))
        analyzed_long.append(
            {
                "run_id": row["run_id"],
                "trial_id": row["trial_id"],
                "configuration_name": row["configuration_name"],
                "block": row["block"],
                "restore_seconds": row.get("restore_seconds"),
                "total_seconds": row.get("total_seconds"),
                "action_seconds": row.get("action_seconds", {}),
                **windows,
            }
        )
    _, manifest = pilot_manifest_payload()
    thresholds = manifest["acceptance_thresholds"]
    comparison = compare_duration_windows(
        analyzed_long,
        maximum_tps_relative_difference=float(thresholds["median_tps_relative"]),
        maximum_p99_absolute_difference_ms=float(thresholds["median_p99_ms_absolute"]),
        minimum_rank_correlation=float(thresholds["rank_correlation"]),
        minimum_pareto_membership_agreement=float(thresholds["pareto_membership_agreement"]),
    )
    short_pairs = []
    for short in short_rows:
        matching = next(
            row
            for row in analyzed_long
            if row["block"] == short["block"]
            and row["configuration_name"] == short["configuration_name"]
        )
        objectives = dict(short.get("objective_values") or {})
        prefix = matching["first_window"]
        short_pairs.append(
            {
                "block": short["block"],
                "short_run_id": short["run_id"],
                "long_run_id": matching["run_id"],
                "tps_relative_difference": abs(
                    float(objectives["throughput_tps"]) - float(prefix["throughput_tps"])
                )
                / max(abs(float(prefix["throughput_tps"])), 1e-9),
                "p99_absolute_difference_ms": abs(
                    float(objectives["p99_ms"]) - float(prefix["p99_ms"])
                ),
            }
        )
    standalone = {
        "pairs": short_pairs,
        "median_tps_relative_difference": statistics.median(
            pair["tps_relative_difference"] for pair in short_pairs
        ),
        "median_p99_absolute_difference_ms": statistics.median(
            pair["p99_absolute_difference_ms"] for pair in short_pairs
        ),
    }
    standalone["passed"] = standalone["median_tps_relative_difference"] <= float(
        thresholds["standalone_tps_relative"]
    ) and standalone["median_p99_absolute_difference_ms"] <= float(
        thresholds["standalone_p99_ms_absolute"]
    )
    payload = {
        "campaign_id": str(campaign_id),
        "pilot_id": str(pilot["pilot_id"]),
        "selected_warmup_seconds": pilot["selected_warmup_seconds"],
        "long_run_comparison": comparison,
        "standalone_short_comparison": standalone,
        "passed": comparison["passed"] and standalone["passed"],
        "recommended_f3_seconds": 300 if comparison["passed"] and standalone["passed"] else 600,
        "long_runs": analyzed_long,
    }
    payload["analysis_sha256"] = _json_sha256(payload)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_warmup_duration_pilots
               SET duration_analysis=%s WHERE campaign_id=%s AND status='COMPLETED'""",
            (Jsonb(payload), campaign_id),
        )
        conn.commit()
    return payload


def pilot_step_dict(step: PilotStep) -> dict[str, Any]:
    return {
        "action": step.action,
        "campaign_id": str(step.campaign_id),
        "pilot_id": str(step.pilot_id),
        "run_id": str(step.run_id) if step.run_id else None,
        "trial_id": str(step.trial_id) if step.trial_id else None,
        "details": step.details,
    }
