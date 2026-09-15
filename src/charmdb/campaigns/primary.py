from __future__ import annotations

import hashlib
import json
import math
import shutil
import statistics
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from psycopg.types.json import Jsonb

from charmdb.campaigns.default_reference import (
    FROZEN_BASELINE_ID,
    FROZEN_CLIENT_THREADS,
    FROZEN_CONCURRENCY,
    FROZEN_MEASUREMENT_SECONDS,
    FROZEN_PREFLIGHT_ID,
    FROZEN_PROFILE_ID,
    FROZEN_RESTORE_MECHANISM,
    FROZEN_WARMUP_SECONDS,
)
from charmdb.config import Settings
from charmdb.controller import discover_knobs, validate_candidate
from charmdb.db import connect
from charmdb.optimization.bo import (
    PRIMARY_REFERENCE_POINT,
    PrimaryBOMethod,
    PrimaryObservation,
    primary_acquisition_name,
    primary_hypervolume,
    recommend_primary_bo,
)
from charmdb.optimization.design import (
    BO_METHODS,
    PRIMARY_MANIFEST,
    PRIMARY_PARAMETER_ORDER,
    build_wave_a_plan,
    primary_candidate_design_sha256,
    primary_restore_soak_contract_sha256,
    schedule_sha256,
)
from charmdb.protocol import PRIMARY_SEARCH_METHODS, load_manifest
from charmdb.reporting.primary import (
    candidate_scatter,
    control_series,
    failure_accounting,
    logical_slot_trajectories,
    pareto_front,
    render_primary_report,
    valid_primary_row,
)
from charmdb.restore.preflight import _target_safety
from charmdb.statistics import descriptive_summary, holm_adjust, paired_comparison
from charmdb.worker import (
    control_campaign,
    create_campaign,
    create_tuned_benchmark_trial,
    run_once,
)

PRIMARY_STAGE = "primary-comparison"
INFRASTRUCTURE_FAILURES = frozenset({"DATASET_RESTORE_FAILED", "BASELINE_FINGERPRINT_FAILED"})
TERMINAL_RUN_STATUSES = frozenset({"COMPLETED", "CANDIDATE_FAILED", "INFRASTRUCTURE_EXHAUSTED"})


@dataclass(frozen=True)
class PrimaryStep:
    action: str
    campaign_id: uuid.UUID
    primary_block_id: uuid.UUID
    primary_run_id: uuid.UUID | None
    trial_id: uuid.UUID | None
    details: dict[str, Any]


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _derived_seed(label: str) -> int:
    return int.from_bytes(hashlib.sha256(label.encode()).digest()[:4], "big") % 2_147_483_647


def _configuration_key(configuration: dict[str, str]) -> str:
    return json.dumps(configuration, sort_keys=True, separators=(",", ":"))


def _proposal_sha256(
    vector: tuple[float, ...] | None, configuration: dict[str, str], method: str
) -> str:
    return _canonical_sha256(
        {
            "method": method,
            "vector": list(vector) if vector is not None else None,
            "configuration": configuration,
        }
    )


def primary_manifest_payload(
    path: Path = PRIMARY_MANIFEST,
) -> tuple[str, dict[str, Any]]:
    manifest = load_manifest(path)
    if manifest.stage != PRIMARY_STAGE or manifest.evidence_role != "PRIMARY":
        raise ValueError("primary execution requires the dedicated PRIMARY manifest")
    payload = manifest.payload
    plan = build_wave_a_plan(path)
    observed_schedule = schedule_sha256(tuple(item.schedule for item in plan))
    expected_schedule = payload["execution_schedule"]["schedule_sha256"]
    if observed_schedule != expected_schedule:
        raise ValueError("primary execution schedule differs from its frozen hash")
    observed_design = primary_candidate_design_sha256(payload)
    expected_design = payload["shared_bo_initial_design"]["candidate_design_sha256"]
    if observed_design != expected_design:
        raise ValueError("primary candidate design differs from its frozen hash")
    if tuple(payload["hypervolume_reference_point"]["value"]) != PRIMARY_REFERENCE_POINT:
        raise ValueError("primary hypervolume reference point must remain (0,-40)")
    retry = dict(payload["infrastructure_retry_policy"])
    if set(retry["retryable_failure_types"]) != set(INFRASTRUCTURE_FAILURES):
        raise ValueError("primary retry failure classification differs from the runner")
    return hashlib.sha256(path.read_bytes()).hexdigest(), payload


def _artifact_disk_headroom(path: Path, required: int) -> dict[str, Any]:
    probe = path.resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return {
        "probe_path": str(probe),
        "free_bytes": usage.free,
        "required_free_bytes": required,
        "passed": usage.free >= required,
    }


def primary_readiness(
    settings: Settings,
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest_path: Path = PRIMARY_MANIFEST,
) -> dict[str, Any]:
    manifest_sha256, payload = primary_manifest_payload(manifest_path)
    if preflight_id != FROZEN_PREFLIGHT_ID:
        raise ValueError("primary comparison must use the frozen scale-500 preflight")
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
               WHERE b.status='PASSED' AND b.baseline_id=%s
                 AND b.benchmark_profile_id=%s
               ORDER BY b.completed_at DESC LIMIT 1""",
            (FROZEN_BASELINE_ID, FROZEN_PROFILE_ID),
        )
        default_reference = cur.fetchone()
        cur.execute(
            """SELECT to_regclass(
                   'charm_control.experiment_v2_primary_blocks'
               ) IS NOT NULL AS installed"""
        )
        schema_row = cur.fetchone()
        primary_schema_installed = bool(schema_row and schema_row["installed"])
        if primary_schema_installed:
            cur.execute(
                """SELECT b.primary_block_id,b.status,c.status AS campaign_status
                   FROM charm_control.experiment_v2_primary_blocks b
                   JOIN charm_control.campaigns c USING(campaign_id)
                   ORDER BY b.created_at DESC LIMIT 1"""
            )
            existing = cur.fetchone()
        else:
            existing = None
        cur.execute(
            """SELECT to_regclass(
                   'charm_control.experiment_v2_primary_restore_soaks'
               ) IS NOT NULL AS installed"""
        )
        soak_schema_row = cur.fetchone()
        soak_schema_installed = bool(soak_schema_row and soak_schema_row["installed"])
        restore_soak = None
        if soak_schema_installed:
            cur.execute(
                """SELECT restore_soak_id,status,required_repetitions,artifact_directory,
                          result,result_sha256,completed_at
                   FROM charm_control.experiment_v2_primary_restore_soaks
                   WHERE contract_sha256=%s AND preflight_id=%s AND baseline_id=%s
                     AND restore_mechanism=%s AND artifact_directory=%s AND status='PASSED'
                   ORDER BY completed_at DESC LIMIT 1""",
                (
                    primary_restore_soak_contract_sha256(payload),
                    FROZEN_PREFLIGHT_ID,
                    FROZEN_BASELINE_ID,
                    FROZEN_RESTORE_MECHANISM,
                    str(settings.artifact_dir.resolve()),
                ),
            )
            restore_soak = cur.fetchone()
    if baseline is None or baseline["approved"] is not True:
        raise ValueError("primary comparison requires the approved scale-500 baseline")
    if uuid.UUID(str(baseline["preflight_id"])) != preflight_id:
        raise ValueError("frozen preflight does not own the primary baseline")
    if baseline["restore_mechanism"] != FROZEN_RESTORE_MECHANISM:
        raise ValueError("primary comparison requires the selected logical restore")
    if default_reference is None or default_reference["campaign_status"] != "STOPPED":
        raise ValueError("primary comparison requires the terminal passed default reference")

    metadata = discover_knobs(settings, set(PRIMARY_PARAMETER_ORDER))
    default_configuration = {
        str(name): str(value)
        for name, value in dict(payload["postgresql_default_configuration"]).items()
    }
    validate_candidate(default_configuration, metadata)
    boot = {str(row["name"]): str(row["boot_val"]) for row in metadata}
    if default_configuration != boot:
        raise ValueError("primary PostgreSQL-default vector differs from boot defaults")
    for item in build_wave_a_plan(manifest_path):
        if item.candidate is not None:
            validate_candidate(item.candidate.configuration, metadata)

    target = _target_safety(settings, require_idle=False)
    artifact_path = settings.artifact_dir.resolve()
    outside_onedrive = "onedrive" not in str(artifact_path).lower()
    environment_gates = dict(payload["pre_campaign_environment_gates"])
    minimum_restore_passes = int(environment_gates["minimum_consecutive_restore_passes"])
    minimum_artifact_free_bytes = int(environment_gates["minimum_artifact_free_bytes"])
    disk = _artifact_disk_headroom(artifact_path, minimum_artifact_free_bytes)
    consecutive_passes = (
        int(dict(restore_soak["result"] or {}).get("consecutive_passes", 0))
        if restore_soak is not None
        else 0
    )
    restore_durations = (
        [float(value) for value in dict(restore_soak["result"] or {}).get("duration_seconds", [])]
        if restore_soak is not None
        else []
    )
    retry_status = payload["infrastructure_retry_policy"]["status"]
    drift_status = payload["drift_interpretation"]["status"]
    blockers = []
    if payload["status"] != "ready" or payload["execution_ready"] is not True:
        blockers.append("manifest-not-authorized")
    if retry_status != "frozen" or drift_status != "frozen":
        blockers.append("p007-retry-and-drift-rules-not-supervisor-frozen")
    if not primary_schema_installed:
        blockers.append("migration-037-not-applied")
    if not soak_schema_installed:
        blockers.append("migration-038-not-applied")
    if existing is not None:
        blockers.append("primary-block-already-exists")
    if target["active_campaigns"] != 0:
        blockers.append("another-campaign-is-active")
    if target["pending_restart"] != 0:
        blockers.append("target-has-pending-restart")
    if target["managed_indexes"] != 0:
        blockers.append("target-has-managed-indexes")
    if target["active_charm_sessions"] != 0:
        blockers.append("target-has-active-charm-sessions")
    if not outside_onedrive:
        blockers.append("artifact-directory-is-inside-onedrive")
    if not disk["passed"]:
        blockers.append("artifact-disk-headroom-below-50-gib")
    if consecutive_passes < minimum_restore_passes:
        blockers.append("restore-reliability-soak-below-15-consecutive-passes")
    return {
        "ready": not blockers,
        "blockers": blockers,
        "manifest_status": payload["status"],
        "execution_ready": payload["execution_ready"],
        "manifest_sha256": manifest_sha256,
        "schedule_sha256": payload["execution_schedule"]["schedule_sha256"],
        "candidate_design_sha256": payload["shared_bo_initial_design"]["candidate_design_sha256"],
        "preflight_id": str(preflight_id),
        "baseline_id": str(FROZEN_BASELINE_ID),
        "benchmark_profile_id": FROZEN_PROFILE_ID,
        "default_reference": {
            "block_id": str(default_reference["block_id"]),
            "status": default_reference["status"],
            "campaign_status": default_reference["campaign_status"],
        },
        "existing_block": dict(existing) if existing is not None else None,
        "primary_schema_installed": primary_schema_installed,
        "target_safety": target,
        "artifact_directory": {
            "path": str(artifact_path),
            "outside_onedrive": outside_onedrive,
            "disk": disk,
        },
        "restore_reliability_gate": {
            "restore_soak_id": (
                str(restore_soak["restore_soak_id"]) if restore_soak is not None else None
            ),
            "recent_consecutive_passes": consecutive_passes,
            "required_minimum": minimum_restore_passes,
            "campaign_grade_evidence": consecutive_passes >= minimum_restore_passes,
            "observed_duration_seconds": restore_durations,
            "result_sha256": restore_soak["result_sha256"] if restore_soak is not None else None,
        },
        "retry_policy_status": retry_status,
        "drift_interpretation_status": drift_status,
    }


def create_primary_plan(
    settings: Settings,
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest_path: Path = PRIMARY_MANIFEST,
) -> uuid.UUID:
    readiness = primary_readiness(settings, preflight_id, manifest_path)
    if readiness["ready"] is not True:
        raise ValueError(
            f"primary execution is blocked by readiness gates: {readiness['blockers']}"
        )
    _, payload = primary_manifest_payload(manifest_path)
    campaign_id = create_campaign(
        settings,
        "v2-primary-comparison-wave-a",
        "PRIMARY",
        {"maximize": "throughput_tps", "minimize": "p99_ms"},
        {
            "learned_constraints": [],
            "hard_safety_gates": payload["hard_safety_gates"],
        },
        failure_limit=393,
        campaign_settings={
            "protocol_id": "thesis-protocol-v2",
            "stage": PRIMARY_STAGE,
            "manifest_sha256": readiness["manifest_sha256"],
            "schedule_sha256": readiness["schedule_sha256"],
            "candidate_design_sha256": readiness["candidate_design_sha256"],
            "preflight_id": str(preflight_id),
            "candidate_restore_baseline_id": str(FROZEN_BASELINE_ID),
            "benchmark_profile_id": FROZEN_PROFILE_ID,
            "manifest": payload,
        },
        actor="v2-primary",
    )
    primary_block_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{campaign_id}:v2-primary-wave-a")
    default_configuration = {
        str(name): str(value)
        for name, value in dict(payload["postgresql_default_configuration"]).items()
    }
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_primary_blocks
               (primary_block_id,campaign_id,protocol_id,evidence_role,manifest_sha256,
                preflight_id,baseline_id,benchmark_profile_id,schedule_sha256,
                candidate_design_sha256,status,retry_policy,drift_interpretation)
               VALUES (%s,%s,'thesis-protocol-v2','PRIMARY',%s,%s,%s,%s,%s,%s,
                       'PLANNED',%s,%s)""",
            (
                primary_block_id,
                campaign_id,
                readiness["manifest_sha256"],
                preflight_id,
                FROZEN_BASELINE_ID,
                FROZEN_PROFILE_ID,
                readiness["schedule_sha256"],
                readiness["candidate_design_sha256"],
                Jsonb(payload["infrastructure_retry_policy"]),
                Jsonb(payload["drift_interpretation"]),
            ),
        )
        for item in build_wave_a_plan(manifest_path):
            entry = item.schedule
            configuration = (
                default_configuration
                if entry.evaluation_role == "DEFAULT_CONTROL"
                else item.candidate.configuration
                if item.candidate is not None
                else None
            )
            vector = item.candidate.vector if item.candidate is not None else None
            status = "PROPOSED" if configuration is not None else "PLANNED"
            proposal_sha = (
                _proposal_sha256(vector, configuration, entry.method)
                if configuration is not None
                else None
            )
            run_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"charmdb:{primary_block_id}:run:{entry.global_position}"
            )
            random_seed = _derived_seed(
                f"thesis-protocol-v2/primary/observation/{entry.seed}/{entry.within_seed_position}"
            )
            cur.execute(
                """INSERT INTO charm_control.experiment_v2_primary_runs
                   (primary_run_id,primary_block_id,campaign_id,global_position,
                    seed_index,seed,within_seed_position,evaluation_role,method,
                    budget_position,shared_with_methods,random_seed,candidate_vector,
                    requested_configuration,proposal_sha256,acquisition_name,status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    run_id,
                    primary_block_id,
                    campaign_id,
                    entry.global_position,
                    entry.seed_index,
                    entry.seed,
                    entry.within_seed_position,
                    entry.evaluation_role,
                    entry.method,
                    entry.budget_position,
                    Jsonb(list(entry.shared_with_methods)),
                    random_seed,
                    Jsonb(list(vector)) if vector is not None else None,
                    Jsonb(configuration) if configuration is not None else None,
                    proposal_sha,
                    primary_acquisition_name(entry.method)
                    if entry.method in PRIMARY_SEARCH_METHODS
                    else None,
                    status,
                ),
            )
        conn.commit()
    return campaign_id


def _primary_block(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT b.*,c.status AS campaign_status
               FROM charm_control.experiment_v2_primary_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.campaign_id=%s""",
            (campaign_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown v2 primary campaign {campaign_id}")
    return dict(row)


def _reconcile_primary_attempt(
    settings: Settings, primary_block_id: uuid.UUID
) -> dict[str, Any] | None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.primary_run_id,r.infrastructure_attempts,
                      a.primary_attempt_id,a.attempt_number,a.trial_id,
                      t.state,t.completed_at,t.failure_type,t.objective_values,
                      t.constraint_values,t.diagnostic_details
               FROM charm_control.experiment_v2_primary_runs r
               JOIN charm_control.experiment_v2_primary_attempts a
                 ON a.primary_run_id=r.primary_run_id AND a.status='CREATED'
               JOIN charm_control.trials t USING(trial_id)
               WHERE r.primary_block_id=%s AND r.status='CREATED'
               ORDER BY r.global_position LIMIT 1""",
            (primary_block_id,),
        )
        row = cur.fetchone()
        if row is None or row["completed_at"] is None:
            return dict(row) if row is not None else None
        result = dict(row)
        failure_type = str(row["failure_type"]) if row["failure_type"] else None
        objectives = dict(row["objective_values"] or {})
        constraints = dict(row["constraint_values"] or {})
        failures = int(constraints.get("failures", 0))
        measurement_valid = (
            row["state"] == "COMPLETED"
            and failures == 0
            and math.isfinite(float(objectives.get("throughput_tps", math.nan)))
            and math.isfinite(float(objectives.get("p99_ms", math.nan)))
        )
        details = {
            "trial_state": row["state"],
            "failure_type": failure_type,
            "failures": failures,
            "diagnostic_details": dict(row["diagnostic_details"] or {}),
        }
        if measurement_valid:
            attempt_status = "COMPLETED"
            run_status = "COMPLETED"
        elif failure_type in INFRASTRUCTURE_FAILURES:
            attempt_status = "INFRASTRUCTURE_FAILED"
            run_status = (
                "RETRY_PENDING" if int(row["attempt_number"]) < 3 else "INFRASTRUCTURE_EXHAUSTED"
            )
        else:
            attempt_status = "CANDIDATE_FAILED"
            run_status = "CANDIDATE_FAILED"
        terminal = run_status in TERMINAL_RUN_STATUSES
        cur.execute(
            """UPDATE charm_control.experiment_v2_primary_attempts
               SET status=%s,failure_type=%s,failure_details=%s,
                   completed_at=clock_timestamp()
               WHERE primary_attempt_id=%s AND status='CREATED'""",
            (
                attempt_status,
                failure_type,
                Jsonb(details),
                row["primary_attempt_id"],
            ),
        )
        cur.execute(
            """UPDATE charm_control.experiment_v2_primary_runs
               SET status=%s,infrastructure_attempts=%s,failure_details=%s,
                   completed_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END
               WHERE primary_run_id=%s AND status='CREATED'""",
            (
                run_status,
                int(row["attempt_number"]),
                Jsonb(details if run_status != "COMPLETED" else {}),
                terminal,
                row["primary_run_id"],
            ),
        )
        result["attempt_status"] = attempt_status
        result["run_status"] = run_status
        conn.commit()
    return result


def _training_observations(
    settings: Settings, row: dict[str, Any]
) -> tuple[list[PrimaryObservation], set[str]]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.primary_run_id,r.candidate_vector,r.requested_configuration,
                      r.status,t.objective_values,t.constraint_values
               FROM charm_control.experiment_v2_primary_runs r
               LEFT JOIN charm_control.experiment_v2_primary_attempts a
                 ON a.primary_run_id=r.primary_run_id AND a.status='COMPLETED'
               LEFT JOIN charm_control.trials t USING(trial_id)
               WHERE r.primary_block_id=%s AND r.seed=%s
                 AND r.global_position < %s
                 AND (r.method='bo_shared_initial' OR r.method=%s)
               ORDER BY r.global_position""",
            (
                row["primary_block_id"],
                row["seed"],
                row["global_position"],
                row["method"],
            ),
        )
        training_rows = [dict(item) for item in cur.fetchall()]
        cur.execute(
            """SELECT requested_configuration
               FROM charm_control.experiment_v2_primary_runs
               WHERE primary_block_id=%s AND seed=%s
                 AND (method='bo_shared_initial' OR method=%s)
                 AND requested_configuration IS NOT NULL""",
            (row["primary_block_id"], row["seed"], row["method"]),
        )
        proposed_rows = cur.fetchall()
    observations: list[PrimaryObservation] = []
    for item in training_rows:
        if item["status"] != "COMPLETED" or item["objective_values"] is None:
            continue
        objectives = dict(item["objective_values"])
        constraints = dict(item["constraint_values"] or {})
        if item["candidate_vector"] is None:
            continue
        observation = PrimaryObservation(
            run_id=uuid.UUID(str(item["primary_run_id"])),
            vector=tuple(float(value) for value in item["candidate_vector"]),
            configuration={
                str(name): str(value)
                for name, value in dict(item["requested_configuration"]).items()
            },
            throughput_tps=float(objectives["throughput_tps"]),
            p99_ms=float(objectives["p99_ms"]),
            failures=int(constraints.get("failures", 0)),
            hard_gates_passed=True,
        )
        if observation.valid:
            observations.append(observation)
    excluded = {
        _configuration_key(
            {str(name): str(value) for name, value in dict(item["requested_configuration"]).items()}
        )
        for item in proposed_rows
    }
    return observations, excluded


def _materialize_adaptive_candidate(
    settings: Settings, row: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    if row["method"] not in BO_METHODS or row["budget_position"] < 13:
        raise ValueError("only adaptive BO slots may be materialized at runtime")
    observations, excluded = _training_observations(settings, row)
    if len(observations) < 4:
        raise ValueError("adaptive primary slot lacks four valid isolated observations")
    seed = _derived_seed(
        f"thesis-protocol-v2/primary/acquisition/{row['seed']}/"
        f"{row['method']}/{row['budget_position']}"
    )
    recommendation = recommend_primary_bo(
        cast(PrimaryBOMethod, row["method"]),
        observations,
        seed,
        excluded,
        payload,
        reference_point=PRIMARY_REFERENCE_POINT,
    )
    candidate = recommendation.candidate
    proposal_sha = _proposal_sha256(candidate.vector, candidate.configuration, row["method"])
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_primary_runs
               SET candidate_vector=%s,requested_configuration=%s,proposal_sha256=%s,
                   acquisition_name=%s,acquisition_value=%s,status='PROPOSED'
               WHERE primary_run_id=%s AND status='PLANNED'""",
            (
                Jsonb(list(candidate.vector)),
                Jsonb(candidate.configuration),
                proposal_sha,
                recommendation.acquisition_name,
                recommendation.acquisition_value,
                row["primary_run_id"],
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError("adaptive primary slot lost its PLANNED state")
        for position, training_id in enumerate(recommendation.training_run_ids, start=1):
            cur.execute(
                """INSERT INTO charm_control.experiment_v2_primary_training_lineage
                   (primary_run_id,training_run_id,training_position)
                   VALUES (%s,%s,%s)""",
                (row["primary_run_id"], training_id, position),
            )
        conn.commit()
    return {
        **row,
        "candidate_vector": list(candidate.vector),
        "requested_configuration": candidate.configuration,
        "proposal_sha256": proposal_sha,
        "acquisition_name": recommendation.acquisition_name,
        "acquisition_value": recommendation.acquisition_value,
        "status": "PROPOSED",
        "training_run_ids": [str(item) for item in recommendation.training_run_ids],
    }


def _pause_for_infrastructure(
    settings: Settings,
    campaign_id: uuid.UUID,
    primary_block_id: uuid.UUID,
    reconciled: dict[str, Any],
    owner: str,
) -> PrimaryStep:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_primary_blocks
               SET status='PAUSED_INFRASTRUCTURE'
               WHERE primary_block_id=%s AND status='RUNNING'""",
            (primary_block_id,),
        )
        conn.commit()
    block = _primary_block(settings, campaign_id)
    if block["campaign_status"] == "RUNNING":
        control_campaign(
            settings,
            campaign_id,
            "pause",
            f"primary slot {reconciled['primary_run_id']} exhausted infrastructure retries",
            actor=owner,
        )
    return PrimaryStep(
        "infrastructure-paused",
        campaign_id,
        primary_block_id,
        uuid.UUID(str(reconciled["primary_run_id"])),
        uuid.UUID(str(reconciled["trial_id"])),
        {
            "failure_type": reconciled.get("failure_type"),
            "attempts": reconciled.get("attempt_number"),
            "candidate_budget_consumed": False,
            "human_decision_required": True,
        },
    )


def _execute_or_resume_attempt(
    settings: Settings,
    campaign_id: uuid.UUID,
    primary_block_id: uuid.UUID,
    row: dict[str, Any],
    owner: str,
    lease_seconds: int,
) -> PrimaryStep:
    run_id = uuid.UUID(str(row["primary_run_id"]))
    attempt_number = int(row["infrastructure_attempts"]) + 1
    configuration = {
        str(name): str(value) for name, value in dict(row["requested_configuration"]).items()
    }
    evaluation_role = {
        "DEFAULT_CONTROL": "PRIMARY_DEFAULT_CONTROL",
        "BO_SHARED_INITIAL": "PRIMARY_BO_SHARED_INITIAL",
        "CANDIDATE": "PRIMARY_CANDIDATE",
    }[str(row["evaluation_role"])]
    trial_id = create_tuned_benchmark_trial(
        settings,
        campaign_id,
        FROZEN_PREFLIGHT_ID,
        configuration,
        int(row["random_seed"]),
        f"v2-primary:{run_id}:attempt:{attempt_number}",
        evidence_role="PRIMARY",
        evaluation_role=evaluation_role,
        warmup_seconds=FROZEN_WARMUP_SECONDS,
        duration_seconds=FROZEN_MEASUREMENT_SECONDS,
        concurrency=FROZEN_CONCURRENCY,
        client_threads=FROZEN_CLIENT_THREADS,
        max_attempts=1,
        restore_mechanism=FROZEN_RESTORE_MECHANISM,
        benchmark_profile=FROZEN_PROFILE_ID,
        runtime_samples_required=True,
    )
    attempt_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"charmdb:{run_id}:primary-attempt:{attempt_number}"
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_primary_attempts
               (primary_attempt_id,primary_run_id,attempt_number,trial_id,status)
               VALUES (%s,%s,%s,%s,'CREATED')
               ON CONFLICT (primary_run_id,attempt_number) DO NOTHING""",
            (attempt_id, run_id, attempt_number, trial_id),
        )
        cur.execute(
            """UPDATE charm_control.experiment_v2_primary_runs
               SET status='CREATED'
               WHERE primary_run_id=%s AND status IN ('PROPOSED','RETRY_PENDING')""",
            (run_id,),
        )
        if cur.rowcount != 1:
            raise RuntimeError("primary run lost its executable state")
        conn.commit()
    result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
    finalized = _reconcile_primary_attempt(settings, primary_block_id)
    if finalized is not None and finalized.get("run_status") == "INFRASTRUCTURE_EXHAUSTED":
        return _pause_for_infrastructure(settings, campaign_id, primary_block_id, finalized, owner)
    if finalized is not None and finalized.get("run_status") == "RETRY_PENDING":
        return PrimaryStep(
            "infrastructure-retry-pending",
            campaign_id,
            primary_block_id,
            run_id,
            trial_id,
            {
                "failure_type": finalized.get("failure_type"),
                "attempt": attempt_number,
                "candidate_reused": True,
                "candidate_budget_consumed": False,
            },
        )
    return PrimaryStep(
        "run-executed",
        campaign_id,
        primary_block_id,
        run_id,
        trial_id,
        {
            "trial_state": result.state,
            "run_status": finalized.get("run_status") if finalized else None,
            "global_position": row["global_position"],
            "method": row["method"],
            "attempt": attempt_number,
        },
    )


def _run_primary_next_with_contract(
    settings: Settings,
    campaign_id: uuid.UUID,
    *,
    payload: dict[str, Any],
    expected_observations: int,
    expected_wave: str,
    owner: str,
    lease_seconds: int,
) -> PrimaryStep:
    block = _primary_block(settings, campaign_id)
    if str(block.get("wave", "A")) != expected_wave:
        raise ValueError(
            f"primary campaign wave must be {expected_wave}, not {block.get('wave', 'A')}"
        )
    primary_block_id = uuid.UUID(str(block["primary_block_id"]))
    reconciled = _reconcile_primary_attempt(settings, primary_block_id)
    if reconciled is not None and reconciled.get("run_status") == "INFRASTRUCTURE_EXHAUSTED":
        return _pause_for_infrastructure(settings, campaign_id, primary_block_id, reconciled, owner)
    if block["campaign_status"] != "RUNNING":
        raise ValueError(f"primary campaign must be RUNNING, not {block['campaign_status']}")
    if block["status"] == "PLANNED":
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_primary_blocks
                   SET status='RUNNING' WHERE primary_block_id=%s AND status='PLANNED'""",
                (primary_block_id,),
            )
            conn.commit()
        block["status"] = "RUNNING"
    if block["status"] != "RUNNING":
        raise ValueError(f"primary block cannot run from {block['status']}")
    if reconciled is not None and reconciled.get("completed_at") is None:
        result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
        finalized = _reconcile_primary_attempt(settings, primary_block_id)
        if finalized is not None and finalized.get("run_status") == "INFRASTRUCTURE_EXHAUSTED":
            return _pause_for_infrastructure(
                settings, campaign_id, primary_block_id, finalized, owner
            )
        return PrimaryStep(
            "run-resumed",
            campaign_id,
            primary_block_id,
            uuid.UUID(str(reconciled["primary_run_id"])),
            result.trial_id,
            {"trial_state": result.state},
        )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_v2_primary_runs
               WHERE primary_block_id=%s
                 AND status IN ('PLANNED','PROPOSED','RETRY_PENDING')
               ORDER BY global_position LIMIT 1""",
            (primary_block_id,),
        )
        planned_row = cur.fetchone()
    if planned_row is None:
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT status,count(*) AS count
                   FROM charm_control.experiment_v2_primary_runs
                   WHERE primary_block_id=%s GROUP BY status""",
                (primary_block_id,),
            )
            counts = {str(item["status"]): int(item["count"]) for item in cur.fetchall()}
            if (
                sum(counts.get(status, 0) for status in TERMINAL_RUN_STATUSES)
                != expected_observations
            ):
                raise RuntimeError(
                    f"primary ledger has no runnable slot but is incomplete: {counts}"
                )
            if counts.get("INFRASTRUCTURE_EXHAUSTED", 0):
                raise RuntimeError("primary ledger contains an infrastructure-exhausted slot")
            cur.execute(
                """UPDATE charm_control.experiment_v2_primary_blocks
                   SET status='OBSERVATIONS_COMPLETE'
                   WHERE primary_block_id=%s AND status='RUNNING'""",
                (primary_block_id,),
            )
            conn.commit()
        control_campaign(
            settings,
            campaign_id,
            "pause",
            "all primary observations are terminal; frozen analysis required",
            actor=owner,
        )
        return PrimaryStep(
            "observations-complete",
            campaign_id,
            primary_block_id,
            None,
            None,
            {"counts": counts},
        )
    row = dict(planned_row)
    if row["status"] == "PLANNED":
        row = _materialize_adaptive_candidate(settings, row, payload)
    return _execute_or_resume_attempt(
        settings, campaign_id, primary_block_id, row, owner, lease_seconds
    )


def run_primary_next(
    settings: Settings,
    campaign_id: uuid.UUID,
    *,
    owner: str = "v2-primary",
    lease_seconds: int = 600,
) -> PrimaryStep:
    _, payload = primary_manifest_payload(PRIMARY_MANIFEST)
    return _run_primary_next_with_contract(
        settings,
        campaign_id,
        payload=payload,
        expected_observations=393,
        expected_wave="A",
        owner=owner,
        lease_seconds=lease_seconds,
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def primary_history(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    block = _primary_block(settings, campaign_id)
    primary_block_id = uuid.UUID(str(block["primary_block_id"]))
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.*,latest.primary_attempt_id,latest.attempt_number,
                      latest.trial_id,latest.attempt_status,t.state,t.objective_values,
                      t.constraint_values,t.failure_type,t.started_at AS trial_started_at,
                      t.completed_at AS trial_completed_at,cr.restore_id,
                      cr.duration_seconds AS restore_seconds,cr.exact_core_passed,
                      cr.physical_statistics_passed
               FROM charm_control.experiment_v2_primary_runs r
               LEFT JOIN LATERAL (
                   SELECT a.primary_attempt_id,a.attempt_number,a.trial_id,
                          a.status AS attempt_status
                   FROM charm_control.experiment_v2_primary_attempts a
                   WHERE a.primary_run_id=r.primary_run_id
                   ORDER BY a.attempt_number DESC LIMIT 1
               ) latest ON true
               LEFT JOIN charm_control.trials t ON t.trial_id=latest.trial_id
               LEFT JOIN charm_control.experiment_candidate_dataset_restores cr
                 ON cr.restore_id=t.candidate_dataset_restore_id
               WHERE r.primary_block_id=%s ORDER BY r.global_position""",
            (primary_block_id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT a.* FROM charm_control.experiment_v2_primary_attempts a
               JOIN charm_control.experiment_v2_primary_runs r USING(primary_run_id)
               WHERE r.primary_block_id=%s
               ORDER BY r.global_position,a.attempt_number""",
            (primary_block_id,),
        )
        attempts = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT l.* FROM charm_control.experiment_v2_primary_training_lineage l
               JOIN charm_control.experiment_v2_primary_runs r
                 ON r.primary_run_id=l.primary_run_id
               WHERE r.primary_block_id=%s
               ORDER BY r.global_position,l.training_position""",
            (primary_block_id,),
        )
        lineage = [dict(row) for row in cur.fetchall()]
    return cast(
        dict[str, Any],
        _json_safe({"block": block, "runs": rows, "attempts": attempts, "lineage": lineage}),
    )


#: Analysis, trajectories, tables, and figures share one validity definition.
_valid_analysis_row = valid_primary_row


def _fitted_endpoint_change(values: list[float], positions: list[int]) -> float:
    x_mean = statistics.fmean(positions)
    y_mean = statistics.fmean(values)
    denominator = sum((position - x_mean) ** 2 for position in positions)
    if denominator == 0:
        return 0.0
    slope = (
        sum(
            (position - x_mean) * (value - y_mean)
            for position, value in zip(positions, values, strict=True)
        )
        / denominator
    )
    return slope * (max(positions) - min(positions))


def _interpolated_control(controls: list[dict[str, float]], position: int, metric: str) -> float:
    before = [item for item in controls if item["position"] <= position]
    after = [item for item in controls if item["position"] >= position]
    left = before[-1] if before else controls[0]
    right = after[0] if after else controls[-1]
    if left["position"] == right["position"]:
        return float(left[metric])
    fraction = (position - left["position"]) / (right["position"] - left["position"])
    return float(left[metric] + fraction * (right[metric] - left[metric]))


def _mean_or_none(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def analyze_primary_observations(
    rows: list[dict[str, Any]],
    payload: dict[str, Any],
    *,
    seeds: list[int] | None = None,
    expected_observations: int = 393,
    report_scope: str | None = None,
) -> dict[str, Any]:
    analysis_seeds = [int(seed) for seed in (seeds or payload["wave_a"]["seeds"])]
    if len(rows) != expected_observations:
        raise ValueError(
            f"primary analysis requires the complete {expected_observations}-slot ledger"
        )
    controls_by_seed: dict[int, list[dict[str, float]]] = {}
    drift_flags: list[dict[str, Any]] = []
    for seed in analysis_seeds:
        controls: list[dict[str, float]] = []
        for row in rows:
            if int(row["seed"]) != int(seed) or row["method"] != "postgresql_default":
                continue
            if not _valid_analysis_row(row):
                continue
            objectives = dict(row["objective_values"])
            controls.append(
                {
                    "position": float(row["within_seed_position"]),
                    "throughput_tps": float(objectives["throughput_tps"]),
                    "p99_ms": float(objectives["p99_ms"]),
                }
            )
        controls.sort(key=lambda item: item["position"])
        controls_by_seed[int(seed)] = controls
        if len(controls) < 2:
            drift_flags.append(
                {"seed": int(seed), "flagged": True, "reason": "fewer-than-two-valid-controls"}
            )
            continue
        positions = [int(item["position"]) for item in controls]
        tps_values = [item["throughput_tps"] for item in controls]
        p99_values = [item["p99_ms"] for item in controls]
        tps_change = _fitted_endpoint_change(tps_values, positions)
        p99_change = _fitted_endpoint_change(p99_values, positions)
        tps_relative = abs(tps_change) / abs(statistics.fmean(tps_values))
        flagged = tps_relative > 0.05 or abs(p99_change) > 5.0
        drift_flags.append(
            {
                "seed": int(seed),
                "valid_controls": len(controls),
                "fitted_tps_change": tps_change,
                "absolute_fitted_tps_change_relative_to_control_mean": tps_relative,
                "fitted_p99_change_ms": p99_change,
                "flagged": flagged,
                "campaign_killing": False,
            }
        )

    expanded: dict[tuple[int, str], list[dict[str, Any]]] = {}
    physical_valid_candidates = 0
    for row in rows:
        if row["method"] == "postgresql_default" or not _valid_analysis_row(row):
            continue
        physical_valid_candidates += 1
        objectives = dict(row["objective_values"])
        seed = int(row["seed"])
        controls = controls_by_seed[seed]
        row_methods = list(BO_METHODS) if row["method"] == "bo_shared_initial" else [row["method"]]
        adjusted: dict[str, float] | None = None
        if controls:
            control_tps = _interpolated_control(
                controls, int(row["within_seed_position"]), "throughput_tps"
            )
            control_p99 = _interpolated_control(
                controls, int(row["within_seed_position"]), "p99_ms"
            )
            adjusted = {
                "control_tps": control_tps,
                "control_p99_ms": control_p99,
                "tps_relative": float(objectives["throughput_tps"]) / control_tps - 1.0,
                "p99_delta_ms": float(objectives["p99_ms"]) - control_p99,
            }
        for method in row_methods:
            expanded.setdefault((seed, method), []).append(
                {
                    "run_id": row["primary_run_id"],
                    "vector": tuple(float(value) for value in row["candidate_vector"]),
                    "configuration": dict(row["requested_configuration"]),
                    "throughput_tps": float(objectives["throughput_tps"]),
                    "p99_ms": float(objectives["p99_ms"]),
                    "adjusted": adjusted,
                }
            )

    seed_method_results: list[dict[str, Any]] = []
    for seed in analysis_seeds:
        for method in PRIMARY_SEARCH_METHODS:
            points = expanded.get((int(seed), method), [])
            observations = [
                PrimaryObservation(
                    uuid.UUID(str(point["run_id"])),
                    tuple(point["vector"]),
                    {str(k): str(v) for k, v in point["configuration"].items()},
                    float(point["throughput_tps"]),
                    float(point["p99_ms"]),
                    0,
                    True,
                )
                for point in points
            ]
            adjusted_points = [
                cast(dict[str, float], point["adjusted"])
                for point in points
                if point["adjusted"] is not None
            ]
            seed_method_results.append(
                {
                    "seed": int(seed),
                    "method": method,
                    "valid_observations": len(points),
                    "expected_budget": 30,
                    "best_throughput_tps": max(
                        (point["throughput_tps"] for point in points), default=None
                    ),
                    "minimum_p99_ms": min((point["p99_ms"] for point in points), default=None),
                    "hypervolume_at_0_negative_40": primary_hypervolume(observations),
                    "mean_control_relative_tps": (
                        statistics.fmean(item["tps_relative"] for item in adjusted_points)
                        if adjusted_points
                        else None
                    ),
                    "mean_control_relative_p99_ms": (
                        statistics.fmean(item["p99_delta_ms"] for item in adjusted_points)
                        if adjusted_points
                        else None
                    ),
                    "observations_beating_control_tps": sum(
                        item["tps_relative"] > 0 for item in adjusted_points
                    ),
                    "observations_beating_control_p99": sum(
                        item["p99_delta_ms"] < 0 for item in adjusted_points
                    ),
                    "observations_dominating_local_control": sum(
                        item["tps_relative"] > 0 and item["p99_delta_ms"] < 0
                        for item in adjusted_points
                    ),
                }
            )
    method_summaries: list[dict[str, Any]] = []
    for method in PRIMARY_SEARCH_METHODS:
        seed_rows = [item for item in seed_method_results if item["method"] == method]
        method_summaries.append(
            {
                "method": method,
                "seed_count": len(seed_rows),
                "valid_observations": sum(item["valid_observations"] for item in seed_rows),
                "mean_final_hypervolume": statistics.fmean(
                    item["hypervolume_at_0_negative_40"] for item in seed_rows
                ),
                "mean_seed_best_throughput_tps": _mean_or_none(
                    [
                        float(item["best_throughput_tps"])
                        for item in seed_rows
                        if item["best_throughput_tps"] is not None
                    ]
                ),
                "mean_seed_minimum_p99_ms": _mean_or_none(
                    [
                        float(item["minimum_p99_ms"])
                        for item in seed_rows
                        if item["minimum_p99_ms"] is not None
                    ]
                ),
                "mean_control_relative_tps": _mean_or_none(
                    [
                        float(item["mean_control_relative_tps"])
                        for item in seed_rows
                        if item["mean_control_relative_tps"] is not None
                    ]
                ),
                "mean_control_relative_p99_ms": _mean_or_none(
                    [
                        float(item["mean_control_relative_p99_ms"])
                        for item in seed_rows
                        if item["mean_control_relative_p99_ms"] is not None
                    ]
                ),
            }
        )
    metric_definitions = {
        "hypervolume_at_0_negative_40": "higher-is-better",
        "best_throughput_tps": "higher-is-better",
        "minimum_p99_ms": "lower-is-better",
        "mean_control_relative_tps": "higher-is-better",
        "mean_control_relative_p99_ms": "lower-is-better",
    }
    seed_level_statistics: list[dict[str, Any]] = []
    pairwise_comparisons: list[dict[str, Any]] = []
    for metric, direction in metric_definitions.items():
        values_by_method: dict[str, list[float]] = {}
        for method in PRIMARY_SEARCH_METHODS:
            values = [
                float(item[metric])
                for item in seed_method_results
                if item["method"] == method and item[metric] is not None
            ]
            values_by_method[method] = values
            if values:
                summary = descriptive_summary(
                    values,
                    seed=_derived_seed(f"thesis-protocol-v2/primary/analysis/{metric}/{method}"),
                )
                seed_level_statistics.append(
                    {
                        "metric": metric,
                        "direction": direction,
                        "method": method,
                        **asdict(summary),
                        "bootstrap_unit": "seed",
                    }
                )
        metric_pairs: list[dict[str, Any]] = []
        for left_index, left in enumerate(PRIMARY_SEARCH_METHODS):
            for right in PRIMARY_SEARCH_METHODS[left_index + 1 :]:
                left_values = values_by_method[left]
                right_values = values_by_method[right]
                if len(left_values) != len(analysis_seeds) or len(right_values) != len(
                    analysis_seeds
                ):
                    continue
                comparison = paired_comparison(left_values, right_values)
                metric_pairs.append(
                    {
                        "metric": metric,
                        "direction": direction,
                        "baseline_method": left,
                        "treatment_method": right,
                        **asdict(comparison),
                        "pairing_unit": "seed",
                    }
                )
        adjusted_p_values = holm_adjust(
            [float(item["permutation_p_value"]) for item in metric_pairs]
        )
        for item, adjusted_p_value in zip(metric_pairs, adjusted_p_values, strict=True):
            item["holm_adjusted_p_value_within_metric_family"] = adjusted_p_value
        pairwise_comparisons.extend(metric_pairs)
    terminal_counts: dict[str, int] = {}
    for row in rows:
        terminal_counts[str(row["status"])] = terminal_counts.get(str(row["status"]), 0) + 1
    any_drift = any(bool(item["flagged"]) for item in drift_flags)
    trajectories = logical_slot_trajectories(rows, payload)
    accounting = failure_accounting(rows, payload)
    front = pareto_front(rows)
    for method_summary in method_summaries:
        counts = next(item for item in accounting if item["method"] == method_summary["method"])
        method_summary["candidate_failed_slots"] = counts["candidate_failed_slots"]
        method_summary["completed_but_invalid_slots"] = counts["completed_but_invalid_slots"]
        method_summary["infrastructure_exhausted_slots"] = counts["infrastructure_exhausted_slots"]
        method_summary["retained_infrastructure_attempts"] = counts[
            "retained_infrastructure_attempts"
        ]
    result = {
        "outcome": "COMPLETE_WITH_DRIFT_FLAGS" if any_drift else "COMPLETE",
        "analysis_policy": payload["drift_interpretation"],
        "reference_point": list(PRIMARY_REFERENCE_POINT),
        "terminal_counts": terminal_counts,
        "physical_valid_candidates": physical_valid_candidates,
        "valid_default_controls": sum(len(items) for items in controls_by_seed.values()),
        "drift_flags": drift_flags,
        "seed_method_results": seed_method_results,
        "method_results": method_summaries,
        "seed_level_statistics": seed_level_statistics,
        "pairwise_comparisons": pairwise_comparisons,
        "slot_trajectories": trajectories,
        "failure_accounting": accounting,
        "pareto_front": front,
        "control_series": control_series(rows, payload),
        "candidate_scatter": candidate_scatter(rows),
        "inference_guard": (
            f"Seed is the independent unit (n={len(analysis_seeds)}). Bootstrap intervals are "
            "descriptive and exact "
            "paired permutation p-values are necessarily coarse; no observation-level "
            "pseudo-replication is used. Holm adjustment is applied within each endpoint family."
        ),
        "interpretation_guard": (
            "Improvement over default is evaluated against interpolated interleaved controls. "
            "Report latency-side or Pareto expansion separately from TPS dominance."
        ),
    }
    if report_scope is not None:
        result.update(
            {
                "report_scope": report_scope,
                "independent_seed_count": len(analysis_seeds),
                "expected_physical_observations": expected_observations,
                "analysis_seeds": analysis_seeds,
            }
        )
    return result


def analyze_primary(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    block = _primary_block(settings, campaign_id)
    if block["status"] == "ANALYZED":
        return dict(block["analysis"])
    if block["status"] != "OBSERVATIONS_COMPLETE" or block["campaign_status"] != "PAUSED":
        raise ValueError("primary analysis requires a paused observations-complete block")
    history = primary_history(settings, campaign_id)
    _, payload = primary_manifest_payload(PRIMARY_MANIFEST)
    analysis = analyze_primary_observations(history["runs"], payload)
    analysis_sha256 = _canonical_sha256(analysis)
    primary_block_id = uuid.UUID(str(block["primary_block_id"]))
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_primary_blocks
               SET status='ANALYZED',analysis=%s,analysis_sha256=%s,
                   completed_at=clock_timestamp()
               WHERE primary_block_id=%s AND status='OBSERVATIONS_COMPLETE'""",
            (Jsonb(analysis), analysis_sha256, primary_block_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("primary block lost its observations-complete state")
        conn.commit()
    control_campaign(
        settings,
        campaign_id,
        "stop",
        f"primary analysis complete with outcome {analysis['outcome']}",
        actor="v2-primary",
    )
    return {**analysis, "analysis_sha256": analysis_sha256}


def export_primary_analysis(
    settings: Settings,
    campaign_id: uuid.UUID,
    output_path: Path | None = None,
) -> dict[str, Any]:
    block = _primary_block(settings, campaign_id)
    if block["status"] != "ANALYZED" or block["analysis"] is None:
        raise ValueError("primary analysis export requires a terminal analyzed block")
    load_manifest(PRIMARY_MANIFEST)
    artifact_root = (settings.artifact_dir / PRIMARY_STAGE).resolve()
    output = output_path or artifact_root / "primary-analysis.json"
    resolved_output = output.resolve()
    if resolved_output != artifact_root and artifact_root not in resolved_output.parents:
        raise ValueError("primary analysis export must stay under the manifest artifact root")
    payload = {
        "campaign_id": str(campaign_id),
        "primary_block_id": str(block["primary_block_id"]),
        "analysis_sha256": block["analysis_sha256"],
        "analysis_sha256_semantics": "canonical JSON payload hash, not file hash",
        "analysis": block["analysis"],
        "history": primary_history(settings, campaign_id),
    }
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return {
        "output_path": str(resolved_output),
        "file_sha256": hashlib.sha256(resolved_output.read_bytes()).hexdigest(),
        "analysis_sha256": block["analysis_sha256"],
    }


def primary_report_context(
    block: dict[str, Any], manifest_path: Path = PRIMARY_MANIFEST
) -> dict[str, Any]:
    manifest_sha256, payload = primary_manifest_payload(manifest_path)
    return {
        "campaign_id": str(block["campaign_id"]),
        "primary_block_id": str(block["primary_block_id"]),
        "evidence_role": "PRIMARY",
        "manifest_sha256": manifest_sha256,
        "schedule_sha256": payload["execution_schedule"]["schedule_sha256"],
        "candidate_design_sha256": payload["shared_bo_initial_design"]["candidate_design_sha256"],
        "benchmark_profile_id": payload["benchmark_profile_id"],
        "wave": "A",
        "seeds": [int(seed) for seed in payload["wave_a"]["seeds"]],
        "analysis_sha256": block["analysis_sha256"],
    }


def export_primary_report(
    settings: Settings,
    campaign_id: uuid.UUID,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Render the frozen Wave A tables and figures from a terminal analysis."""
    block = _primary_block(settings, campaign_id)
    if block["status"] != "ANALYZED" or block["analysis"] is None:
        raise ValueError("primary report requires a terminal analyzed block")
    artifact_root = (settings.artifact_dir / PRIMARY_STAGE).resolve()
    output = (output_dir or artifact_root / "report").resolve()
    if output != artifact_root and artifact_root not in output.parents:
        raise ValueError("primary report must stay under the manifest artifact root")
    return render_primary_report(dict(block["analysis"]), primary_report_context(block), output)


def primary_step_dict(step: PrimaryStep) -> dict[str, Any]:
    return cast(dict[str, Any], _json_safe(asdict(step)))
