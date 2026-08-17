from __future__ import annotations

import hashlib
import json
import math
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
from charmdb.worker import (
    control_campaign,
    create_campaign,
    create_v2_tuned_benchmark_trial,
    run_once,
)

DEFAULT_REFERENCE_MANIFEST = Path("v2/config/new-machine-default-reference.json")
SATURATION_MANIFEST = Path("v2/config/sanity-and-saturation.json")
FROZEN_PROFILE_ID = "scale500-c32-w600-f3-600-v1"
FROZEN_PREFLIGHT_ID = uuid.UUID("137d0027-c256-59fa-b583-4277e6461486")
FROZEN_BASELINE_ID = uuid.UUID("35459275-c9bf-544f-be54-4e3537464c78")
FROZEN_WARMUP_SECONDS = 600
FROZEN_MEASUREMENT_SECONDS = 600
FROZEN_CONCURRENCY = 32
FROZEN_CLIENT_THREADS = 4
FROZEN_RESTORE_MECHANISM = "logical-restore"
FROZEN_MAINTENANCE_POLICY = "canonical-baseline-only-no-vacuum"
INITIAL_RUNS = 5
CONTINGENCY_RUNS = 5

_T_CRITICAL_95 = {5: 2.7764451051977987, 10: 2.2621571627409915}


@dataclass(frozen=True)
class DefaultReferenceStep:
    action: str
    campaign_id: uuid.UUID
    block_id: uuid.UUID
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
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _positive_int_list(value: Any, label: str, count: int) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != count
        or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in value)
        or len(set(value)) != count
    ):
        raise ValueError(f"{label} must contain {count} unique positive integer seeds")
    return value


def default_reference_manifest_payload(
    path: Path = DEFAULT_REFERENCE_MANIFEST,
) -> tuple[str, dict[str, Any]]:
    manifest = load_manifest(path)
    if manifest.stage != "new-machine-default-reference":
        raise ValueError("default-reference runner requires its dedicated v2 manifest")
    payload = manifest.payload
    profile = dict(payload.get("benchmark_profile") or {})
    expected_profile = {
        "id": FROZEN_PROFILE_ID,
        "warmup_seconds": FROZEN_WARMUP_SECONDS,
        "measurement_seconds": FROZEN_MEASUREMENT_SECONDS,
        "concurrency": FROZEN_CONCURRENCY,
        "client_threads": FROZEN_CLIENT_THREADS,
        "restore_mechanism": FROZEN_RESTORE_MECHANISM,
        "pgbench_maintenance_policy": FROZEN_MAINTENANCE_POLICY,
    }
    mismatches = {
        key: {"expected": expected, "observed": profile.get(key)}
        for key, expected in expected_profile.items()
        if profile.get(key) != expected
    }
    if mismatches:
        raise ValueError(f"default-reference manifest differs from frozen profile: {mismatches}")
    if payload.get("evaluation_role") != "DEFAULT_CONTROL":
        raise ValueError("default-reference observations must use DEFAULT_CONTROL")
    primary = _positive_int_list(payload.get("primary_seeds"), "primary_seeds", INITIAL_RUNS)
    contingency = _positive_int_list(
        payload.get("contingency_seeds"), "contingency_seeds", CONTINGENCY_RUNS
    )
    if set(primary).intersection(contingency):
        raise ValueError("primary and contingency default-reference seeds must be disjoint")
    criteria = dict(payload.get("acceptance_criteria") or {})
    expected_criteria = {
        "confidence_level": 0.95,
        "maximum_tps_relative_ci_half_width": 0.05,
        "maximum_p99_ci_half_width_ms": 5.0,
        "maximum_tps_fitted_block_change_relative": 0.05,
        "maximum_p99_fitted_block_change_ms": 5.0,
        "required_valid_observations": 5,
        "maximum_total_observations": 10,
        "benchmark_failures_max": 0,
    }
    criteria_mismatches = {
        key: {"expected": expected, "observed": criteria.get(key)}
        for key, expected in expected_criteria.items()
        if criteria.get(key) != expected
    }
    if criteria_mismatches:
        raise ValueError(
            f"default-reference criteria differ from the implementation: {criteria_mismatches}"
        )
    return _sha256(path), payload


def _default_configuration(path: Path = SATURATION_MANIFEST) -> dict[str, str]:
    manifest = load_manifest(path)
    phase2 = dict(manifest.payload.get("phase_2") or {})
    configurations = phase2.get("representative_configurations")
    if not isinstance(configurations, list):
        raise ValueError("saturation manifest has no Phase 2 configurations")
    matches = [item for item in configurations if item.get("name") == "postgresql_default"]
    if len(matches) != 1 or not isinstance(matches[0].get("values"), dict):
        raise ValueError("frozen Phase 2 design must contain one PostgreSQL-default vector")
    return {str(key): str(value) for key, value in matches[0]["values"].items()}


def default_reference_readiness(
    settings: Settings,
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest_path: Path = DEFAULT_REFERENCE_MANIFEST,
) -> dict[str, Any]:
    manifest_sha256, manifest = default_reference_manifest_payload(manifest_path)
    if preflight_id != FROZEN_PREFLIGHT_ID:
        raise ValueError("default-reference block must use the frozen scale-500 preflight")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT preflight_id,restore_mechanism,approved
               FROM charm_control.experiment_candidate_dataset_baselines
               WHERE baseline_id=%s""",
            (FROZEN_BASELINE_ID,),
        )
        baseline = cur.fetchone()
        cur.execute(
            """SELECT b.block_id,b.status,c.status AS campaign_status
               FROM charm_control.experiment_v2_default_reference_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               ORDER BY b.created_at DESC LIMIT 1"""
        )
        existing = cur.fetchone()
    if baseline is None or baseline["approved"] is not True:
        raise ValueError("default-reference block requires the approved scale-500 baseline")
    if uuid.UUID(str(baseline["preflight_id"])) != preflight_id:
        raise ValueError("frozen preflight does not own the selected candidate baseline")
    if baseline["restore_mechanism"] != FROZEN_RESTORE_MECHANISM:
        raise ValueError("default-reference block requires the selected logical restore")
    target = _target_safety(settings)
    configuration = _default_configuration()
    metadata = discover_knobs(settings, set(configuration))
    validate_candidate(configuration, metadata)
    boot = {str(row["name"]): str(row["boot_val"]) for row in metadata}
    if configuration != boot:
        raise ValueError("pre-registered PostgreSQL-default vector differs from boot defaults")
    return {
        "ready": (
            manifest.get("status") == "ready"
            and manifest.get("restore_inclusive_runtime_accepted") is True
            and existing is None
        ),
        "manifest_status": manifest.get("status"),
        "restore_inclusive_runtime_accepted": manifest.get(
            "restore_inclusive_runtime_accepted", False
        ),
        "manifest_sha256": manifest_sha256,
        "preflight_id": str(preflight_id),
        "baseline_id": str(FROZEN_BASELINE_ID),
        "benchmark_profile_id": FROZEN_PROFILE_ID,
        "initial_observations": INITIAL_RUNS,
        "maximum_contingency_observations": CONTINGENCY_RUNS,
        "existing_block": (
            {
                "block_id": str(existing["block_id"]),
                "status": str(existing["status"]),
                "campaign_status": str(existing["campaign_status"]),
            }
            if existing is not None
            else None
        ),
        "target_safety": target,
    }


def create_default_reference_plan(
    settings: Settings,
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest_path: Path = DEFAULT_REFERENCE_MANIFEST,
) -> uuid.UUID:
    readiness = default_reference_readiness(settings, preflight_id, manifest_path)
    if readiness["ready"] is not True:
        raise ValueError(
            "default-reference execution is blocked until runtime is accepted and no block exists"
        )
    _, manifest = default_reference_manifest_payload(manifest_path)
    campaign_id = create_campaign(
        settings,
        "v2-new-machine-default-reference",
        "CALIBRATION",
        {"maximize": "throughput_tps", "minimize": "p99_ms"},
        {"failures": 0, "p99_slo_applied": False},
        failure_limit=1,
        campaign_settings={
            "protocol_id": "thesis-protocol-v2",
            "stage": "new-machine-default-reference",
            "manifest_sha256": readiness["manifest_sha256"],
            "preflight_id": str(preflight_id),
            "candidate_restore_baseline_id": str(FROZEN_BASELINE_ID),
            "candidate_restore_gate": "required",
            "restore_mechanism": FROZEN_RESTORE_MECHANISM,
            "benchmark_profile_id": FROZEN_PROFILE_ID,
            "pgbench_maintenance_policy": FROZEN_MAINTENANCE_POLICY,
            "manifest": manifest,
        },
        actor="v2-default-reference",
    )
    block_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{campaign_id}:v2-default-reference")
    primary_seeds = list(manifest["primary_seeds"])
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_default_reference_blocks
               (block_id,campaign_id,protocol_id,evidence_role,manifest_sha256,
                preflight_id,baseline_id,benchmark_profile_id,status,primary_seeds,
                contingency_seeds,acceptance_criteria)
               VALUES (%s,%s,'thesis-protocol-v2','CALIBRATION',%s,%s,%s,%s,
                       'INITIAL_PLANNED',%s,%s,%s)""",
            (
                block_id,
                campaign_id,
                readiness["manifest_sha256"],
                preflight_id,
                FROZEN_BASELINE_ID,
                FROZEN_PROFILE_ID,
                Jsonb(primary_seeds),
                Jsonb(manifest["contingency_seeds"]),
                Jsonb(manifest["acceptance_criteria"]),
            ),
        )
        for sequence, seed in enumerate(primary_seeds, start=1):
            run_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"charmdb:{block_id}:initial:{sequence}:{seed}",
            )
            cur.execute(
                """INSERT INTO charm_control.experiment_v2_default_reference_runs
                   (run_id,block_id,campaign_id,sequence,chronological_execution_index,
                    subphase,random_seed,status)
                   VALUES (%s,%s,%s,%s,%s,'INITIAL',%s,'PLANNED')""",
                (run_id, block_id, campaign_id, sequence, sequence, seed),
            )
        conn.commit()
    return campaign_id


def _block(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT b.*,c.status AS campaign_status
               FROM charm_control.experiment_v2_default_reference_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.campaign_id=%s""",
            (campaign_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown v2 default-reference campaign {campaign_id}")
    return dict(row)


def _reconcile_run(settings: Settings, block_id: uuid.UUID) -> dict[str, Any] | None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.run_id,r.trial_id,r.status,t.state,t.completed_at,t.failure_type
               FROM charm_control.experiment_v2_default_reference_runs r
               JOIN charm_control.trials t USING(trial_id)
               WHERE r.block_id=%s AND r.status='CREATED'
               ORDER BY r.sequence LIMIT 1""",
            (block_id,),
        )
        row = cur.fetchone()
        if row is None or row["completed_at"] is None:
            return dict(row) if row is not None else None
        passed = row["state"] == "COMPLETED"
        status = "COMPLETED" if passed else "FAILED"
        failure_details = (
            {} if passed else {"trial_state": row["state"], "failure_type": row["failure_type"]}
        )
        cur.execute(
            """UPDATE charm_control.experiment_v2_default_reference_runs
               SET status=%s,failure_details=%s,completed_at=clock_timestamp()
               WHERE run_id=%s AND status='CREATED'""",
            (status, Jsonb(failure_details), row["run_id"]),
        )
        conn.commit()
    return {**dict(row), "run_status": status}


def _fail_block(
    settings: Settings,
    campaign_id: uuid.UUID,
    block_id: uuid.UUID,
    reconciled: dict[str, Any],
    owner: str,
) -> DefaultReferenceStep:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_default_reference_blocks
               SET status='FAILED',completed_at=clock_timestamp()
               WHERE block_id=%s AND status NOT IN ('PASSED','BLOCKED','FAILED')""",
            (block_id,),
        )
        conn.commit()
    block = _block(settings, campaign_id)
    if block["campaign_status"] in {"RUNNING", "PAUSED"}:
        control_campaign(
            settings,
            campaign_id,
            "stop",
            f"default-reference trial {reconciled['trial_id']} failed",
            actor=owner,
        )
    return DefaultReferenceStep(
        "run-failed",
        campaign_id,
        block_id,
        uuid.UUID(str(reconciled["run_id"])),
        uuid.UUID(str(reconciled["trial_id"])),
        {"trial_state": reconciled["state"]},
    )


def run_default_reference_next(
    settings: Settings,
    campaign_id: uuid.UUID,
    *,
    owner: str = "v2-default-reference",
    lease_seconds: int = 600,
) -> DefaultReferenceStep:
    block = _block(settings, campaign_id)
    block_id = uuid.UUID(str(block["block_id"]))
    reconciled = _reconcile_run(settings, block_id)
    if reconciled is not None and reconciled.get("run_status") == "FAILED":
        return _fail_block(settings, campaign_id, block_id, reconciled, owner)
    if block["campaign_status"] != "RUNNING":
        raise ValueError(
            f"default-reference campaign must be RUNNING, not {block['campaign_status']}"
        )
    transitions = {
        "INITIAL_PLANNED": "INITIAL_RUNNING",
        "CONTINGENCY_PLANNED": "CONTINGENCY_RUNNING",
    }
    if block["status"] in transitions:
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_default_reference_blocks
                   SET status=%s WHERE block_id=%s AND status=%s""",
                (transitions[block["status"]], block_id, block["status"]),
            )
            conn.commit()
        block["status"] = transitions[block["status"]]
    if block["status"] not in {"INITIAL_RUNNING", "CONTINGENCY_RUNNING"}:
        raise ValueError(f"default-reference block cannot run from {block['status']}")
    if reconciled is not None and reconciled.get("completed_at") is None:
        result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
        finalized = _reconcile_run(settings, block_id)
        if finalized is not None and finalized.get("run_status") == "FAILED":
            return _fail_block(settings, campaign_id, block_id, finalized, owner)
        return DefaultReferenceStep(
            "run-executed",
            campaign_id,
            block_id,
            uuid.UUID(str(reconciled["run_id"])),
            result.trial_id,
            {"trial_state": result.state},
        )
    subphase = "INITIAL" if block["status"] == "INITIAL_RUNNING" else "CONTINGENCY"
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_v2_default_reference_runs
               WHERE block_id=%s AND subphase=%s AND status='PLANNED'
               ORDER BY sequence LIMIT 1""",
            (block_id, subphase),
        )
        planned_row = cur.fetchone()
    if planned_row is None:
        next_status = "INITIAL_COMPLETE" if subphase == "INITIAL" else "CONTINGENCY_COMPLETE"
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_default_reference_blocks
                   SET status=%s WHERE block_id=%s AND status=%s""",
                (next_status, block_id, block["status"]),
            )
            conn.commit()
        control_campaign(
            settings,
            campaign_id,
            "pause",
            f"default-reference {subphase.lower()} observations complete; analysis required",
            actor=owner,
        )
        return DefaultReferenceStep(
            "subphase-complete", campaign_id, block_id, None, None, {"subphase": subphase}
        )
    planned = dict(planned_row)
    run_id = uuid.UUID(str(planned["run_id"]))
    trial_id = create_v2_tuned_benchmark_trial(
        settings,
        campaign_id,
        uuid.UUID(str(block["preflight_id"])),
        _default_configuration(),
        int(planned["random_seed"]),
        f"v2-default-reference:{run_id}",
        evidence_role="CALIBRATION",
        evaluation_role="DEFAULT_CONTROL",
        warmup_seconds=FROZEN_WARMUP_SECONDS,
        duration_seconds=FROZEN_MEASUREMENT_SECONDS,
        concurrency=FROZEN_CONCURRENCY,
        client_threads=FROZEN_CLIENT_THREADS,
        restore_mechanism=FROZEN_RESTORE_MECHANISM,
        benchmark_profile=FROZEN_PROFILE_ID,
        runtime_samples_required=True,
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_default_reference_runs
               SET status='CREATED',trial_id=%s WHERE run_id=%s AND status='PLANNED'""",
            (trial_id, run_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("default-reference run lost its PLANNED state")
        conn.commit()
    result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
    finalized = _reconcile_run(settings, block_id)
    if finalized is not None and finalized.get("run_status") == "FAILED":
        return _fail_block(settings, campaign_id, block_id, finalized, owner)
    return DefaultReferenceStep(
        "run-executed",
        campaign_id,
        block_id,
        run_id,
        trial_id,
        {
            "trial_state": result.state,
            "subphase": subphase,
            "chronological_execution_index": planned["chronological_execution_index"],
        },
    )


def default_reference_history(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    block = _block(settings, campaign_id)
    block_id = uuid.UUID(str(block["block_id"]))
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.*,t.state,t.objective_values,t.constraint_values,t.workflow_result,
                      t.started_at AS trial_started_at,t.completed_at AS trial_completed_at,
                      cr.restore_id,cr.duration_seconds AS restore_seconds,
                      cr.exact_core_passed,cr.physical_statistics_passed
               FROM charm_control.experiment_v2_default_reference_runs r
               LEFT JOIN charm_control.trials t USING(trial_id)
               LEFT JOIN charm_control.experiment_candidate_dataset_restores cr
                 ON cr.restore_id=t.candidate_dataset_restore_id
               WHERE r.block_id=%s ORDER BY r.sequence""",
            (block_id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        for key, value in tuple(row.items()):
            if isinstance(value, uuid.UUID):
                row[key] = str(value)
        if row.get("trial_started_at") is not None and row.get("trial_completed_at") is not None:
            row["total_seconds"] = (
                row["trial_completed_at"] - row["trial_started_at"]
            ).total_seconds()
    block_payload = {
        key: str(value) if isinstance(value, uuid.UUID) else value for key, value in block.items()
    }
    return {"block": block_payload, "runs": rows}


def _linear_slope(values: list[float]) -> float:
    midpoint = (len(values) - 1) / 2
    denominator = sum((index - midpoint) ** 2 for index in range(len(values)))
    return (
        sum(
            (index - midpoint) * (value - statistics.mean(values))
            for index, value in enumerate(values)
        )
        / denominator
    )


def _metric_summary(values: list[float], *, relative: bool) -> dict[str, float]:
    count = len(values)
    mean = statistics.mean(values)
    standard_deviation = statistics.stdev(values)
    ci_half_width = _T_CRITICAL_95[count] * standard_deviation / math.sqrt(count)
    slope = _linear_slope(values)
    fitted_block_change = abs(slope) * (count - 1)
    result = {
        "mean": mean,
        "median": statistics.median(values),
        "minimum": min(values),
        "maximum": max(values),
        "sample_standard_deviation": standard_deviation,
        "ci_95_half_width": ci_half_width,
        "fitted_slope_per_observation": slope,
        "fitted_block_change": fitted_block_change,
    }
    if relative:
        scale = max(abs(mean), 1e-12)
        result["ci_95_relative_half_width"] = ci_half_width / scale
        result["fitted_block_change_relative"] = fitted_block_change / scale
    return result


def analyze_reference_points(
    points: list[dict[str, Any]], criteria: dict[str, Any]
) -> dict[str, Any]:
    if len(points) not in _T_CRITICAL_95:
        raise ValueError("default-reference analysis requires exactly five or ten observations")
    ordered = sorted(points, key=lambda item: int(item["chronological_execution_index"]))
    expected_indexes = list(range(1, len(ordered) + 1))
    observed_indexes = [int(item["chronological_execution_index"]) for item in ordered]
    if observed_indexes != expected_indexes:
        raise ValueError("default-reference chronological indexes are incomplete or reordered")
    tps = [float(item["throughput_tps"]) for item in ordered]
    p99 = [float(item["p99_ms"]) for item in ordered]
    failures = [int(item.get("failures", 0)) for item in ordered]
    if any(not math.isfinite(value) or value <= 0 for value in [*tps, *p99]):
        raise ValueError("default-reference objectives must be finite and positive")
    tps_summary = _metric_summary(tps, relative=True)
    p99_summary = _metric_summary(p99, relative=False)
    validity_passed = all(
        failures_value <= int(criteria["benchmark_failures_max"])
        and bool(point.get("exact_core_passed"))
        and bool(point.get("physical_statistics_passed"))
        for point, failures_value in zip(ordered, failures, strict=True)
    )
    precision = {
        "tps_passed": tps_summary["ci_95_relative_half_width"]
        <= float(criteria["maximum_tps_relative_ci_half_width"]),
        "p99_passed": p99_summary["ci_95_half_width"]
        <= float(criteria["maximum_p99_ci_half_width_ms"]),
    }
    precision["passed"] = precision["tps_passed"] and precision["p99_passed"]
    drift = {
        "tps_passed": tps_summary["fitted_block_change_relative"]
        <= float(criteria["maximum_tps_fitted_block_change_relative"]),
        "p99_passed": p99_summary["fitted_block_change"]
        <= float(criteria["maximum_p99_fitted_block_change_ms"]),
    }
    drift["passed"] = drift["tps_passed"] and drift["p99_passed"]
    if not validity_passed:
        outcome = "BLOCKED_VALIDITY"
    elif not drift["passed"]:
        outcome = "BLOCKED_DRIFT"
    elif precision["passed"]:
        outcome = "PASSED"
    elif len(ordered) == INITIAL_RUNS:
        outcome = "CONTINGENCY_REQUIRED"
    else:
        outcome = "BLOCKED_IMPRECISION"
    return {
        "observation_count": len(ordered),
        "confidence_level": criteria["confidence_level"],
        "validity_passed": validity_passed,
        "precision": precision,
        "drift": drift,
        "throughput_tps": tps_summary,
        "p99_ms": p99_summary,
        "outcome": outcome,
        "points": ordered,
    }


def analyze_default_reference(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    history = default_reference_history(settings, campaign_id)
    block = history["block"]
    if block.get("qualification_purpose") is not None:
        raise ValueError("qualified default-reference blocks require their dedicated analyzer")
    if block["campaign_status"] != "PAUSED":
        raise ValueError("default-reference analysis requires a clean paused campaign")
    if block["status"] not in {"INITIAL_COMPLETE", "CONTINGENCY_COMPLETE"}:
        raise ValueError("default-reference analysis requires a completed subphase")
    rows = [row for row in history["runs"] if row["status"] == "COMPLETED"]
    expected = INITIAL_RUNS if block["status"] == "INITIAL_COMPLETE" else 10
    if len(rows) != expected:
        raise ValueError(f"default-reference analysis requires {expected} completed runs")
    points = []
    for row in rows:
        objectives = dict(row.get("objective_values") or {})
        constraints = dict(row.get("constraint_values") or {})
        points.append(
            {
                "run_id": row["run_id"],
                "trial_id": row["trial_id"],
                "random_seed": row["random_seed"],
                "chronological_execution_index": row["chronological_execution_index"],
                "throughput_tps": float(objectives["throughput_tps"]),
                "p99_ms": float(objectives["p99_ms"]),
                "failures": int(constraints.get("failures", 0)),
                "restore_id": row.get("restore_id"),
                "restore_seconds": row.get("restore_seconds"),
                "exact_core_passed": row.get("exact_core_passed"),
                "physical_statistics_passed": row.get("physical_statistics_passed"),
                "total_seconds": row.get("total_seconds"),
            }
        )
    analysis = {
        "campaign_id": str(campaign_id),
        "block_id": str(block["block_id"]),
        "benchmark_profile_id": block["benchmark_profile_id"],
        "acceptance_criteria": block["acceptance_criteria"],
        **analyze_reference_points(points, dict(block["acceptance_criteria"])),
    }
    analysis["analysis_sha256"] = _json_sha256(analysis)
    outcome = analysis["outcome"]
    block_id = uuid.UUID(str(block["block_id"]))
    terminal_status: str | None = None
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        if block["status"] == "INITIAL_COMPLETE":
            if outcome == "CONTINGENCY_REQUIRED":
                contingency = list(block["contingency_seeds"])
                for sequence, seed in enumerate(contingency, start=6):
                    run_id = uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"charmdb:{block_id}:contingency:{sequence}:{seed}",
                    )
                    cur.execute(
                        """INSERT INTO charm_control.experiment_v2_default_reference_runs
                           (run_id,block_id,campaign_id,sequence,
                            chronological_execution_index,subphase,random_seed,status)
                           VALUES (%s,%s,%s,%s,%s,'CONTINGENCY',%s,'PLANNED')""",
                        (run_id, block_id, campaign_id, sequence, sequence, seed),
                    )
                cur.execute(
                    """UPDATE charm_control.experiment_v2_default_reference_blocks
                       SET status='CONTINGENCY_PLANNED',initial_analysis=%s,
                           initial_analysis_sha256=%s WHERE block_id=%s""",
                    (Jsonb(analysis), analysis["analysis_sha256"], block_id),
                )
            else:
                terminal_status = "PASSED" if outcome == "PASSED" else "BLOCKED"
                cur.execute(
                    """UPDATE charm_control.experiment_v2_default_reference_blocks
                       SET status=%s,initial_analysis=%s,initial_analysis_sha256=%s,
                           final_analysis=%s,final_analysis_sha256=%s,
                           completed_at=clock_timestamp() WHERE block_id=%s""",
                    (
                        terminal_status,
                        Jsonb(analysis),
                        analysis["analysis_sha256"],
                        Jsonb(analysis),
                        analysis["analysis_sha256"],
                        block_id,
                    ),
                )
        else:
            terminal_status = "PASSED" if outcome == "PASSED" else "BLOCKED"
            cur.execute(
                """UPDATE charm_control.experiment_v2_default_reference_blocks
                   SET status=%s,final_analysis=%s,final_analysis_sha256=%s,
                       completed_at=clock_timestamp() WHERE block_id=%s""",
                (
                    terminal_status,
                    Jsonb(analysis),
                    analysis["analysis_sha256"],
                    block_id,
                ),
            )
        conn.commit()
    if terminal_status is not None:
        control_campaign(
            settings,
            campaign_id,
            "stop",
            f"default-reference launch block resolved as {terminal_status}",
            actor="v2-default-reference-analysis",
        )
    analysis["block_status"] = terminal_status or "CONTINGENCY_PLANNED"
    analysis["next_action"] = (
        "explicitly resume the same campaign for the fixed five-observation contingency"
        if terminal_status is None
        else (
            "default-reference gate passed; proceed to parameter-screening pre-registration"
            if terminal_status == "PASSED"
            else "investigate the failed launch condition before any parameter screening"
        )
    )
    return analysis


def export_default_reference_analysis(
    settings: Settings,
    campaign_id: uuid.UUID,
    output: Path | None = None,
) -> dict[str, str]:
    block = _block(settings, campaign_id)
    if block.get("qualification_purpose") is not None:
        raise ValueError("qualified default-reference blocks require their dedicated exporter")
    analysis = block.get("final_analysis")
    analysis_sha256 = block.get("final_analysis_sha256")
    if block["status"] not in {"PASSED", "BLOCKED"} or not isinstance(analysis, dict):
        raise ValueError("default-reference export requires a finalized analysis")
    if not isinstance(analysis_sha256, str) or len(analysis_sha256) != 64:
        raise ValueError("default-reference analysis has no valid persisted SHA-256")
    artifact_root = settings.artifact_dir.resolve()
    destination = (
        output.resolve()
        if output is not None
        else (
            artifact_root
            / "default-reference"
            / f"default-reference-analysis-{block['block_id']}.json"
        ).resolve()
    )
    try:
        destination.relative_to(artifact_root)
    except ValueError as error:
        raise ValueError(
            "default-reference analysis export must stay under artifact root"
        ) from error
    encoded = json.dumps(analysis, indent=2, sort_keys=True, default=str) + "\n"
    hash_payload = dict(analysis)
    embedded_sha256 = hash_payload.pop("analysis_sha256", None)
    if embedded_sha256 != analysis_sha256:
        raise ValueError("embedded default-reference analysis hash differs from its ledger")
    if (
        hashlib.sha256(
            json.dumps(hash_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        != analysis_sha256
    ):
        raise ValueError("persisted default-reference analysis hash does not verify")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(destination)
    return {
        "campaign_id": str(campaign_id),
        "block_id": str(block["block_id"]),
        "analysis_sha256": analysis_sha256,
        "output_path": str(destination),
    }


def default_reference_step_dict(step: DefaultReferenceStep) -> dict[str, Any]:
    return {
        "action": step.action,
        "campaign_id": str(step.campaign_id),
        "block_id": str(step.block_id),
        "run_id": str(step.run_id) if step.run_id else None,
        "trial_id": str(step.trial_id) if step.trial_id else None,
        "details": step.details,
    }
