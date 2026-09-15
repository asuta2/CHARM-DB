from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from psycopg.types.json import Jsonb

from charmdb.campaigns.default_reference import (
    FROZEN_BASELINE_ID,
    FROZEN_CLIENT_THREADS,
    FROZEN_CONCURRENCY,
    FROZEN_PREFLIGHT_ID,
    FROZEN_RESTORE_MECHANISM,
)
from charmdb.config import Settings
from charmdb.controller import discover_knobs, validate_candidate
from charmdb.db import connect
from charmdb.optimization.design import PRIMARY_MANIFEST
from charmdb.protocol import load_manifest
from charmdb.restore.preflight import _target_safety
from charmdb.statistics import descriptive_summary
from charmdb.worker import (
    control_campaign,
    create_campaign,
    create_v2_tuned_benchmark_trial,
    run_once,
)

F4_MANIFEST = Path("experiments/thesis/manifests/f4-confirmation.json")
F4_STAGE = "f4-confirmation"
PRIMARY_ANALYSIS_SHA256 = "fa6263830e5081a64943c1057083450076a32279a191bb889dc66944cbda8f21"
PHASE_B_CAMPAIGN_ID = uuid.UUID("4579eb26-8848-4372-a0e8-785d47a7994f")
INFRASTRUCTURE_FAILURES = frozenset({"DATASET_RESTORE_FAILED", "BASELINE_FINGERPRINT_FAILED"})
TERMINAL_RUN_STATUSES = frozenset({"COMPLETED", "CANDIDATE_FAILED", "INFRASTRUCTURE_EXHAUSTED"})


@dataclass(frozen=True)
class F4Slot:
    physical_position: int
    repetition_block: int
    within_block_position: int
    treatment: str
    source_primary_run_id: uuid.UUID | None
    random_seed: int
    configuration: dict[str, str]


@dataclass(frozen=True)
class F4Step:
    action: str
    campaign_id: uuid.UUID
    f4_block_id: uuid.UUID
    f4_run_id: uuid.UUID | None
    trial_id: uuid.UUID | None
    details: dict[str, Any]


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _proposal_sha256(slot: F4Slot) -> str:
    return _canonical_sha256(
        {
            "repetition_block": slot.repetition_block,
            "within_block_position": slot.within_block_position,
            "treatment": slot.treatment,
            "source_primary_run_id": slot.source_primary_run_id,
            "random_seed": slot.random_seed,
            "configuration": slot.configuration,
        }
    )


def f4_manifest_payload(manifest_path: Path = F4_MANIFEST) -> tuple[str, dict[str, Any]]:
    manifest = load_manifest(manifest_path)
    if manifest.stage != F4_STAGE or manifest.evidence_role != "F4_CONFIRMATION":
        raise ValueError("F4 manifest has the wrong stage or evidence role")
    return _file_sha256(manifest_path), manifest.payload


def build_f4_plan(
    manifest_path: Path = F4_MANIFEST, primary_manifest_path: Path = PRIMARY_MANIFEST
) -> tuple[F4Slot, ...]:
    _, payload = f4_manifest_payload(manifest_path)
    primary = load_manifest(primary_manifest_path).payload
    defaults = {
        str(name): str(value)
        for name, value in dict(primary["postgresql_default_configuration"]).items()
    }
    finalists = {str(row["label"]): dict(row) for row in payload["finalists"]}
    slots: list[F4Slot] = []
    position = 0
    for block in payload["recommended_design"]["schedule"]:
        block_number = int(block["block"])
        seed = int(block["seed"])
        for within_position, treatment in enumerate(block["order"], start=1):
            position += 1
            label = str(treatment)
            finalist = finalists.get(label)
            source_id = (
                uuid.UUID(str(finalist["primary_run_id"])) if finalist is not None else None
            )
            configuration = (
                {str(k): str(v) for k, v in finalist["requested_configuration"].items()}
                if finalist is not None
                else defaults
            )
            slots.append(
                F4Slot(
                    position,
                    block_number,
                    within_position,
                    label,
                    source_id,
                    seed,
                    configuration,
                )
            )
    expected_labels = {"DEFAULT", "T", "L", "H", "E"}
    if len(slots) != 20:
        raise ValueError("F4 design must contain exactly 20 physical observations")
    for block in range(1, 5):
        rows = [slot for slot in slots if slot.repetition_block == block]
        if len(rows) != 5 or {slot.treatment for slot in rows} != expected_labels:
            raise ValueError(f"F4 block {block} is not complete")
        if len({slot.random_seed for slot in rows}) != 1:
            raise ValueError(f"F4 block {block} does not use one common workload seed")
    return tuple(slots)


def _source_gate(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    source = payload["source_evidence"]
    analysis_path = settings.artifact_dir / "primary-comparison" / "primary-analysis.json"
    pareto_path = (
        settings.artifact_dir
        / "primary-comparison"
        / "report"
        / "tables"
        / "pareto-front.csv"
    )
    analysis_file_sha = _file_sha256(analysis_path) if analysis_path.is_file() else None
    pareto_file_sha = _file_sha256(pareto_path) if pareto_path.is_file() else None
    finalist_rows_match = False
    if pareto_file_sha == source["pareto_table_file_sha256"]:
        with pareto_path.open("r", encoding="utf-8", newline="") as handle:
            pareto_rows = {row["primary_run_id"]: row for row in csv.DictReader(handle)}
        finalist_rows_match = all(
            finalist["primary_run_id"] in pareto_rows
            and json.loads(pareto_rows[finalist["primary_run_id"]]["requested_configuration"])
            == finalist["requested_configuration"]
            and math.isclose(
                float(pareto_rows[finalist["primary_run_id"]]["throughput_tps"]),
                float(finalist["wave_a_throughput_tps"]),
                rel_tol=1e-12,
            )
            and math.isclose(
                float(pareto_rows[finalist["primary_run_id"]]["p99_ms"]),
                float(finalist["wave_a_p99_ms"]),
                rel_tol=1e-12,
            )
            for finalist in payload["finalists"]
        )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT b.status,c.status AS campaign_status,b.analysis_sha256
               FROM charm_control.experiment_v2_primary_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.campaign_id=%s""",
            (source["primary_campaign_id"],),
        )
        primary = cur.fetchone()
        cur.execute(
            """SELECT b.status,c.status AS campaign_status,b.analysis_sha256
               FROM charm_control.experiment_v2_multifidelity_phase_b_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.campaign_id=%s""",
            (PHASE_B_CAMPAIGN_ID,),
        )
        phase_b = cur.fetchone()
    passed = bool(
        primary
        and primary["status"] == "ANALYZED"
        and primary["campaign_status"] == "STOPPED"
        and str(primary["analysis_sha256"]) == PRIMARY_ANALYSIS_SHA256
        and phase_b
        and phase_b["status"] == "ANALYZED"
        and phase_b["campaign_status"] == "STOPPED"
        and analysis_file_sha == source["primary_analysis_file_sha256"]
        and pareto_file_sha == source["pareto_table_file_sha256"]
        and finalist_rows_match
    )
    return {
        "passed": passed,
        "primary": dict(primary) if primary else None,
        "phase_b": dict(phase_b) if phase_b else None,
        "analysis_file_sha256": analysis_file_sha,
        "pareto_table_sha256": pareto_file_sha,
        "finalist_rows_match": finalist_rows_match,
    }


def _target_fixture_gate(settings: Settings) -> dict[str, Any]:
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT to_regclass('public.charm_index_fixture')::text AS fixture,
                      to_regclass('public.charm_index_fixture_id_seq')::text AS sequence"""
        )
        row = dict(cur.fetchone() or {})
    return {**row, "passed": row.get("fixture") is None and row.get("sequence") is None}


def f4_readiness(settings: Settings, manifest_path: Path = F4_MANIFEST) -> dict[str, Any]:
    manifest_sha256, payload = f4_manifest_payload(manifest_path)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('charm_control.experiment_v2_f4_blocks') IS NOT NULL AS installed"
        )
        schema_installed = bool(cur.fetchone()["installed"])  # type: ignore[index]
        existing = None
        if schema_installed:
            cur.execute(
                """SELECT b.f4_block_id,b.status,c.status AS campaign_status
                   FROM charm_control.experiment_v2_f4_blocks b
                   JOIN charm_control.campaigns c USING(campaign_id)
                   ORDER BY b.created_at DESC LIMIT 1"""
            )
            existing = cur.fetchone()
    plan = build_f4_plan(manifest_path)
    metadata = discover_knobs(settings, set(plan[0].configuration))
    for slot in plan:
        validate_candidate(slot.configuration, metadata)
    target = _target_safety(settings, require_idle=False)
    artifact_path = settings.artifact_dir.resolve()
    usage = shutil.disk_usage(artifact_path)
    source_gate = _source_gate(settings, payload)
    fixture_gate = _target_fixture_gate(settings)
    blockers: list[str] = []
    if payload["status"] != "ready" or payload.get("execution_ready") is not True:
        blockers.append("manifest-not-execution-ready")
    prerequisites = payload["prerequisites"]
    if prerequisites.get("p010_supervisor_confirmation_recorded") is not True:
        blockers.append("p010-not-recorded")
    if prerequisites.get("durable_f4_runner_implemented") is not True:
        blockers.append("durable-runner-not-frozen")
    if prerequisites.get("result_blind_f4_analysis_implemented") is not True:
        blockers.append("result-blind-analysis-not-frozen")
    if not schema_installed:
        blockers.append("migration-040-not-applied")
    if not source_gate["passed"]:
        blockers.append("source-evidence-not-authenticated")
    if not fixture_gate["passed"]:
        blockers.append("target-has-index-integration-test-fixture")
    if existing is not None:
        blockers.append("f4-block-already-exists")
    for key, blocker in (
        ("active_campaigns", "another-campaign-is-active"),
        ("pending_restart", "target-has-pending-restart"),
        ("managed_indexes", "target-has-managed-indexes"),
        ("active_charm_sessions", "target-has-active-charm-sessions"),
    ):
        if target[key] != 0:
            blockers.append(blocker)
    if "onedrive" in str(artifact_path).lower():
        blockers.append("artifact-directory-is-inside-onedrive")
    if usage.free < 50 * 1024**3:
        blockers.append("artifact-disk-headroom-below-50-gib")
    return {
        "ready": not blockers,
        "blockers": blockers,
        "manifest_sha256": manifest_sha256,
        "manifest_status": payload["status"],
        "execution_ready": payload.get("execution_ready"),
        "schema_installed": schema_installed,
        "source_gate": source_gate,
        "test_fixture_gate": fixture_gate,
        "slots": len(plan),
        "existing_block": dict(existing) if existing else None,
        "target_safety": target,
        "artifact_directory": str(artifact_path),
        "artifact_free_bytes": usage.free,
    }


def create_f4_plan(settings: Settings, manifest_path: Path = F4_MANIFEST) -> uuid.UUID:
    readiness = f4_readiness(settings, manifest_path)
    if readiness["ready"] is not True:
        raise ValueError(f"F4 is blocked by readiness gates: {readiness['blockers']}")
    _, payload = f4_manifest_payload(manifest_path)
    source = payload["source_evidence"]
    campaign_id = create_campaign(
        settings,
        "v2-f4-confirmation",
        "F4_CONFIRMATION",
        {"maximize": "throughput_tps", "minimize": "p99_ms"},
        {"benchmark_failures_must_equal": 0, "p99_slo_applied": False},
        failure_limit=20,
        campaign_settings={
            "protocol_id": "thesis-protocol-v2",
            "stage": F4_STAGE,
            "manifest_sha256": readiness["manifest_sha256"],
            "manifest": payload,
        },
        actor="v2-f4-confirmation",
    )
    block_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{campaign_id}:f4-confirmation")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_f4_blocks
               (f4_block_id,campaign_id,protocol_id,evidence_role,manifest_sha256,
                primary_analysis_sha256,pareto_table_sha256,preflight_id,baseline_id,
                benchmark_profile_id,status)
               VALUES (%s,%s,'thesis-protocol-v2','F4_CONFIRMATION',%s,%s,%s,%s,%s,%s,'PLANNED')""",
            (
                block_id,
                campaign_id,
                readiness["manifest_sha256"],
                source["primary_analysis_payload_sha256"],
                source["pareto_table_file_sha256"],
                FROZEN_PREFLIGHT_ID,
                FROZEN_BASELINE_ID,
                payload["benchmark_profile"]["profile_id"],
            ),
        )
        for slot in build_f4_plan(manifest_path):
            run_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"charmdb:{block_id}:run:{slot.physical_position}"
            )
            cur.execute(
                """INSERT INTO charm_control.experiment_v2_f4_runs
                   (f4_run_id,f4_block_id,campaign_id,physical_position,repetition_block,
                    within_block_position,treatment,source_primary_run_id,random_seed,
                    requested_configuration,proposal_sha256,status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PROPOSED')""",
                (
                    run_id,
                    block_id,
                    campaign_id,
                    slot.physical_position,
                    slot.repetition_block,
                    slot.within_block_position,
                    slot.treatment,
                    slot.source_primary_run_id,
                    slot.random_seed,
                    Jsonb(slot.configuration),
                    _proposal_sha256(slot),
                ),
            )
        conn.commit()
    return campaign_id


def _f4_block(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT b.*,c.status AS campaign_status
               FROM charm_control.experiment_v2_f4_blocks b
               JOIN charm_control.campaigns c USING(campaign_id) WHERE b.campaign_id=%s""",
            (campaign_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown F4 campaign {campaign_id}")
    return dict(row)


def _reconcile_attempt(settings: Settings, block_id: uuid.UUID) -> dict[str, Any] | None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.f4_run_id,a.f4_attempt_id,a.attempt_number,a.trial_id,
                      t.state,t.completed_at,t.failure_type,t.objective_values,
                      t.constraint_values,t.diagnostic_details
               FROM charm_control.experiment_v2_f4_runs r
               JOIN charm_control.experiment_v2_f4_attempts a
                 ON a.f4_run_id=r.f4_run_id AND a.status='CREATED'
               JOIN charm_control.trials t USING(trial_id)
               WHERE r.f4_block_id=%s AND r.status='CREATED'
               ORDER BY r.physical_position LIMIT 1""",
            (block_id,),
        )
        row = cur.fetchone()
        if row is None or row["completed_at"] is None:
            return dict(row) if row else None
        failure_type = str(row["failure_type"]) if row["failure_type"] else None
        objectives = dict(row["objective_values"] or {})
        constraints = dict(row["constraint_values"] or {})
        failures = int(constraints.get("failures", 0))
        valid = bool(
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
        if valid:
            attempt_status, run_status = "COMPLETED", "COMPLETED"
        elif failure_type in INFRASTRUCTURE_FAILURES:
            attempt_status = "INFRASTRUCTURE_FAILED"
            run_status = (
                "RETRY_PENDING" if int(row["attempt_number"]) < 3 else "INFRASTRUCTURE_EXHAUSTED"
            )
        else:
            attempt_status, run_status = "CANDIDATE_FAILED", "CANDIDATE_FAILED"
        terminal = run_status in TERMINAL_RUN_STATUSES
        cur.execute(
            """UPDATE charm_control.experiment_v2_f4_attempts
               SET status=%s,failure_type=%s,failure_details=%s,completed_at=clock_timestamp()
               WHERE f4_attempt_id=%s AND status='CREATED'""",
            (attempt_status, failure_type, Jsonb(details), row["f4_attempt_id"]),
        )
        cur.execute(
            """UPDATE charm_control.experiment_v2_f4_runs
               SET status=%s,infrastructure_attempts=%s,failure_details=%s,
                   completed_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END
               WHERE f4_run_id=%s AND status='CREATED'""",
            (
                run_status,
                int(row["attempt_number"]),
                Jsonb(details if run_status != "COMPLETED" else {}),
                terminal,
                row["f4_run_id"],
            ),
        )
        result = {**dict(row), "attempt_status": attempt_status, "run_status": run_status}
        conn.commit()
    return result


def _execute_slot(
    settings: Settings,
    campaign_id: uuid.UUID,
    block_id: uuid.UUID,
    row: dict[str, Any],
    owner: str,
    lease_seconds: int,
) -> F4Step:
    run_id = uuid.UUID(str(row["f4_run_id"]))
    attempt_number = int(row["infrastructure_attempts"]) + 1
    trial_id = create_v2_tuned_benchmark_trial(
        settings,
        campaign_id,
        FROZEN_PREFLIGHT_ID,
        {str(k): str(v) for k, v in dict(row["requested_configuration"]).items()},
        int(row["random_seed"]),
        f"v2-f4:{run_id}:attempt:{attempt_number}",
        evidence_role="F4_CONFIRMATION",
        evaluation_role=f"F4_{row['treatment']}",
        warmup_seconds=600,
        duration_seconds=600,
        concurrency=FROZEN_CONCURRENCY,
        client_threads=FROZEN_CLIENT_THREADS,
        max_attempts=1,
        restore_mechanism=FROZEN_RESTORE_MECHANISM,
        benchmark_profile="scale500-c32-w600-f4-600-v1",
        runtime_samples_required=True,
    )
    attempt_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{run_id}:f4-attempt:{attempt_number}")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_f4_attempts
               (f4_attempt_id,f4_run_id,attempt_number,trial_id,status)
               VALUES (%s,%s,%s,%s,'CREATED')
               ON CONFLICT (f4_run_id,attempt_number) DO NOTHING""",
            (attempt_id, run_id, attempt_number, trial_id),
        )
        cur.execute(
            """UPDATE charm_control.experiment_v2_f4_runs SET status='CREATED'
               WHERE f4_run_id=%s AND status IN ('PROPOSED','RETRY_PENDING')""",
            (run_id,),
        )
        if cur.rowcount != 1:
            raise RuntimeError("F4 slot lost its executable state")
        conn.commit()
    result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
    finalized = _reconcile_attempt(settings, block_id)
    return F4Step(
        "slot-executed",
        campaign_id,
        block_id,
        run_id,
        trial_id,
        {
            "physical_position": row["physical_position"],
            "treatment": row["treatment"],
            "attempt": attempt_number,
            "trial_state": result.state,
            "run_status": finalized.get("run_status") if finalized else None,
        },
    )


def run_f4_next(
    settings: Settings,
    campaign_id: uuid.UUID,
    *,
    owner: str = "v2-f4-confirmation",
    lease_seconds: int = 600,
) -> F4Step:
    block = _f4_block(settings, campaign_id)
    block_id = uuid.UUID(str(block["f4_block_id"]))
    reconciled = _reconcile_attempt(settings, block_id)
    if reconciled is not None and reconciled.get("run_status") == "INFRASTRUCTURE_EXHAUSTED":
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_f4_blocks SET status='PAUSED_INFRASTRUCTURE'
                   WHERE f4_block_id=%s AND status='RUNNING'""",
                (block_id,),
            )
            conn.commit()
        control_campaign(settings, campaign_id, "pause", "F4 retries exhausted", actor=owner)
        return F4Step(
            "infrastructure-paused",
            campaign_id,
            block_id,
            uuid.UUID(str(reconciled["f4_run_id"])),
            uuid.UUID(str(reconciled["trial_id"])),
            {"human_decision_required": True, "repetition_consumed": False},
        )
    if block["campaign_status"] != "RUNNING":
        raise ValueError(f"F4 campaign must be RUNNING, not {block['campaign_status']}")
    if block["status"] == "PLANNED":
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE charm_control.experiment_v2_f4_blocks SET status='RUNNING' "
                "WHERE f4_block_id=%s AND status='PLANNED'",
                (block_id,),
            )
            conn.commit()
    elif block["status"] != "RUNNING":
        raise ValueError(f"F4 block cannot run from {block['status']}")
    if reconciled is not None and reconciled.get("completed_at") is None:
        result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
        _reconcile_attempt(settings, block_id)
        return F4Step("slot-resumed", campaign_id, block_id, None, result.trial_id, {})
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_v2_f4_runs
               WHERE f4_block_id=%s AND status IN ('PROPOSED','RETRY_PENDING')
               ORDER BY physical_position LIMIT 1""",
            (block_id,),
        )
        row = cur.fetchone()
    if row is not None:
        return _execute_slot(settings, campaign_id, block_id, dict(row), owner, lease_seconds)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status,count(*) AS count FROM charm_control.experiment_v2_f4_runs "
            "WHERE f4_block_id=%s GROUP BY status",
            (block_id,),
        )
        counts = {str(item["status"]): int(item["count"]) for item in cur.fetchall()}
        if sum(counts.get(status, 0) for status in TERMINAL_RUN_STATUSES) != 20:
            raise RuntimeError(f"F4 has no runnable slot but is incomplete: {counts}")
        if counts.get("INFRASTRUCTURE_EXHAUSTED", 0):
            raise RuntimeError("F4 contains an infrastructure-exhausted slot")
        cur.execute(
            """UPDATE charm_control.experiment_v2_f4_blocks SET status='OBSERVATIONS_COMPLETE'
               WHERE f4_block_id=%s AND status='RUNNING'""",
            (block_id,),
        )
        conn.commit()
    control_campaign(
        settings, campaign_id, "pause", "all F4 observations are terminal", actor=owner
    )
    return F4Step("observations-complete", campaign_id, block_id, None, None, {"counts": counts})


def f4_history(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    block = _f4_block(settings, campaign_id)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.*,a.attempt_number,a.trial_id,a.status AS attempt_status,
                      t.state AS trial_state,t.objective_values,t.constraint_values
               FROM charm_control.experiment_v2_f4_runs r
               LEFT JOIN LATERAL (
                   SELECT * FROM charm_control.experiment_v2_f4_attempts a
                   WHERE a.f4_run_id=r.f4_run_id ORDER BY attempt_number DESC LIMIT 1
               ) a ON true LEFT JOIN charm_control.trials t USING(trial_id)
               WHERE r.f4_block_id=%s ORDER BY r.physical_position""",
            (block["f4_block_id"],),
        )
        runs = [dict(row) for row in cur.fetchall()]
    return {"block": block, "runs": runs}


def _artifact_path(root: Path, relative: object) -> Path:
    value = Path(str(relative))
    if value.is_absolute():
        raise ValueError("durable artifact path must be relative")
    resolved_root = root.resolve()
    resolved = (resolved_root / value).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("durable artifact path escapes the configured root")
    return resolved


def _load_authenticated_rows(
    settings: Settings, campaign_id: uuid.UUID
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    block = _f4_block(settings, campaign_id)
    if block["status"] != "OBSERVATIONS_COMPLETE" or block["campaign_status"] != "PAUSED":
        raise ValueError("F4 analysis requires a paused observations-complete block")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.*,a.attempt_number,a.status AS attempt_status,a.trial_id,
                      t.state AS trial_state,t.objective_values,t.constraint_values,
                      t.workflow_result,
                      EXTRACT(EPOCH FROM (t.completed_at-t.created_at)) AS lifecycle_seconds,
                      restore.duration_seconds AS restore_seconds,restore.exact_core_passed,
                      restore.physical_statistics_passed,measurement.result AS measurement_result,
                      validation.result AS validation_result,
                      artifact.relative_path AS artifact_relative_path,
                      artifact.sha256 AS artifact_sha256,artifact.byte_size AS artifact_byte_size
               FROM charm_control.experiment_v2_f4_runs r
               JOIN LATERAL (
                   SELECT * FROM charm_control.experiment_v2_f4_attempts a
                   WHERE a.f4_run_id=r.f4_run_id ORDER BY attempt_number DESC LIMIT 1
               ) a ON true JOIN charm_control.trials t USING(trial_id)
               LEFT JOIN charm_control.experiment_candidate_dataset_restores restore
                 ON restore.restore_id=t.candidate_dataset_restore_id
               LEFT JOIN charm_control.trial_action_executions measurement
                 ON measurement.trial_id=t.trial_id AND measurement.state='RUNNING_FULL_EVALUATION'
                AND measurement.status='COMPLETED'
               LEFT JOIN charm_control.trial_action_executions validation
                 ON validation.trial_id=t.trial_id AND validation.state='VALIDATING_MEASUREMENT'
                AND validation.status='COMPLETED'
               LEFT JOIN charm_control.artifacts artifact
                 ON artifact.trial_id=t.trial_id AND artifact.kind='durable-trial-json'
               WHERE r.f4_block_id=%s ORDER BY r.physical_position""",
            (block["f4_block_id"],),
        )
        rows = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT r.physical_position,a.attempt_number,a.status,a.failure_type,t.trial_id,
                      EXTRACT(EPOCH FROM (t.completed_at-t.created_at)) AS lifecycle_seconds
               FROM charm_control.experiment_v2_f4_attempts a
               JOIN charm_control.experiment_v2_f4_runs r USING(f4_run_id)
               JOIN charm_control.trials t USING(trial_id)
               WHERE r.f4_block_id=%s AND a.status='INFRASTRUCTURE_FAILED'
               ORDER BY r.physical_position,a.attempt_number""",
            (block["f4_block_id"],),
        )
        failed_attempts = [dict(row) for row in cur.fetchall()]
    if len(rows) != 20:
        raise ValueError("F4 analysis requires all 20 terminal slots")
    root = settings.artifact_dir.resolve()
    for row, expected in zip(rows, build_f4_plan(), strict=True):
        identity = (
            int(row["physical_position"]),
            int(row["repetition_block"]),
            int(row["within_block_position"]),
            str(row["treatment"]),
            uuid.UUID(str(row["source_primary_run_id"]))
            if row["source_primary_run_id"]
            else None,
            int(row["random_seed"]),
            {str(k): str(v) for k, v in dict(row["requested_configuration"]).items()},
            str(row["proposal_sha256"]),
        )
        expected_identity = (
            expected.physical_position,
            expected.repetition_block,
            expected.within_block_position,
            expected.treatment,
            expected.source_primary_run_id,
            expected.random_seed,
            expected.configuration,
            _proposal_sha256(expected),
        )
        if identity != expected_identity:
            raise ValueError(f"F4 frozen-plan mismatch at position {expected.physical_position}")
        row["authenticated"] = True
        row["valid"] = False
        if row["status"] != "COMPLETED":
            continue
        objectives = dict(row["objective_values"] or {})
        constraints = dict(row["constraint_values"] or {})
        measurement = dict(row["measurement_result"] or {})
        validation = dict(row["validation_result"] or {})
        workflow = dict(row["workflow_result"] or {})
        result = dict(measurement.get("result") or {})
        if not all(
            (
                row["attempt_status"] == "COMPLETED",
                row["trial_state"] == "COMPLETED",
                bool(row["exact_core_passed"]),
                bool(row["physical_statistics_passed"]),
                validation.get("valid") is True,
                int(constraints.get("failures", -1)) == 0,
                int(result.get("failures", -1)) == 0,
            )
        ):
            raise ValueError(f"F4 hard-gate mismatch at position {expected.physical_position}")
        for name in ("throughput_tps", "p99_ms"):
            if not math.isfinite(float(objectives.get(name, math.nan))):
                raise ValueError(f"F4 invalid {name} at position {expected.physical_position}")
        artifact = _artifact_path(root, row["artifact_relative_path"])
        if (
            not artifact.is_file()
            or artifact.stat().st_size != int(row["artifact_byte_size"])
            or _file_sha256(artifact) != str(row["artifact_sha256"])
        ):
            raise ValueError(f"F4 artifact authentication failed: {artifact}")
        artifact_payload = json.loads(artifact.read_text(encoding="utf-8"))
        marker_relative = measurement.get("marker_relative_path")
        if (
            not marker_relative
            or workflow.get("measurement_marker") != marker_relative
            or artifact_payload.get("measurement_marker") != marker_relative
        ):
            raise ValueError("F4 measurement marker lineage mismatch")
        marker = _artifact_path(root, marker_relative)
        if not marker.is_file() or _file_sha256(marker) != str(measurement.get("marker_sha256")):
            raise ValueError(f"F4 marker authentication failed: {marker}")
        row.update(
            {
                "objective_values": objectives,
                "constraint_values": constraints,
                "workflow_result": workflow,
                "valid": True,
                "marker_relative_path": str(marker_relative),
                "marker_sha256": str(measurement["marker_sha256"]),
            }
        )
    return rows, failed_attempts


def _summary(values: list[float], seed: int) -> dict[str, Any]:
    return asdict(descriptive_summary(values, bootstrap_samples=10_000, seed=seed))


def analyze_f4_observations(
    rows: list[dict[str, Any]], failed_attempts: list[dict[str, Any]], payload: dict[str, Any]
) -> dict[str, Any]:
    if len(rows) != 20 or not all(row.get("authenticated") is True for row in rows):
        raise ValueError("F4 analysis requires 20 authenticated terminal slots")
    by_block = {
        block: [row for row in rows if int(row["repetition_block"]) == block]
        for block in range(1, 5)
    }
    if any(len(block_rows) != 5 for block_rows in by_block.values()):
        raise ValueError("F4 analysis requires four complete five-treatment blocks")
    all_valid = all(row.get("valid") is True for row in rows)
    controls = {
        block: next((row for row in block_rows if row["treatment"] == "DEFAULT"), None)
        for block, block_rows in by_block.items()
    }
    all_valid = all_valid and all(
        control is not None and control.get("valid") for control in controls.values()
    )
    summaries: list[dict[str, Any]] = []
    if all_valid:
        for label in ("T", "L", "H", "E"):
            treatment_rows = sorted(
                (row for row in rows if row["treatment"] == label),
                key=lambda row: int(row["repetition_block"]),
            )
            raw_tps = [float(row["objective_values"]["throughput_tps"]) for row in treatment_rows]
            raw_p99 = [float(row["objective_values"]["p99_ms"]) for row in treatment_rows]
            relative_tps = []
            for row in treatment_rows:
                control = controls[int(row["repetition_block"])]
                assert control is not None
                relative_tps.append(
                    (
                        float(row["objective_values"]["throughput_tps"])
                        / float(control["objective_values"]["throughput_tps"])
                        - 1.0
                    )
                    * 100.0
                )
            relative_p99 = [
                float(row["objective_values"]["p99_ms"])
                - float(controls[int(row["repetition_block"])]["objective_values"]["p99_ms"])  # type: ignore[index]
                for row in treatment_rows
            ]
            source_id = str(treatment_rows[0]["source_primary_run_id"])
            summaries.append(
                {
                    "treatment": label,
                    "source_primary_run_id": source_id,
                    "configuration": dict(treatment_rows[0]["requested_configuration"]),
                    "raw_throughput_tps": _summary(raw_tps, 20260904),
                    "raw_p99_ms": _summary(raw_p99, 20260905),
                    "control_relative_tps_percent": _summary(relative_tps, 20260906),
                    "control_relative_p99_ms": _summary(relative_p99, 20260907),
                }
            )
    highest_tps = max(
        (float(row["raw_throughput_tps"]["mean"]) for row in summaries), default=math.nan
    )
    eligible: list[dict[str, Any]] = []
    for row in summaries:
        gates = {
            "hard_gate": True,
            "throughput_vs_default": float(row["control_relative_tps_percent"]["mean"]) >= -5.0,
            "latency_vs_default": float(row["control_relative_p99_ms"]["mean"]) <= -1.0,
            "frontier_throughput": float(row["raw_throughput_tps"]["mean"]) >= 0.95 * highest_tps,
        }
        row["gates"] = gates
        row["eligible"] = all(gates.values())
        if row["eligible"]:
            eligible.append(row)
    selected = (
        min(
            eligible,
            key=lambda row: (
                float(row["raw_p99_ms"]["mean"]),
                -float(row["raw_throughput_tps"]["mean"]),
                str(row["source_primary_run_id"]),
            ),
        )
        if all_valid and eligible
        else None
    )
    successful_seconds = sum(float(row.get("lifecycle_seconds") or 0.0) for row in rows)
    failed_seconds = sum(float(row.get("lifecycle_seconds") or 0.0) for row in failed_attempts)
    return {
        "decision": "CONFIRMED_CHAMPION" if selected else "NO_CONFIRMED_CHAMPION",
        "evidence_role": "F4_CONFIRMATION",
        "all_twenty_observations_valid": all_valid,
        "selected_treatment": selected["treatment"] if selected else None,
        "selected_primary_run_id": selected["source_primary_run_id"] if selected else None,
        "selected_configuration": selected["configuration"] if selected else None,
        "candidate_summaries": summaries,
        "observations": [
            {
                "physical_position": int(row["physical_position"]),
                "repetition_block": int(row["repetition_block"]),
                "within_block_position": int(row["within_block_position"]),
                "treatment": str(row["treatment"]),
                "source_primary_run_id": str(row["source_primary_run_id"] or ""),
                "random_seed": int(row["random_seed"]),
                "valid": bool(row.get("valid")),
                "throughput_tps": (
                    float(row["objective_values"]["throughput_tps"]) if row.get("valid") else None
                ),
                "p99_ms": float(row["objective_values"]["p99_ms"]) if row.get("valid") else None,
                "status": str(row["status"]),
                "attempts": int(row["infrastructure_attempts"]),
            }
            for row in rows
        ],
        "counts": {
            "physical_slots": len(rows),
            "valid_slots": sum(bool(row.get("valid")) for row in rows),
            "candidate_failures": sum(row["status"] == "CANDIDATE_FAILED" for row in rows),
            "retained_infrastructure_failures": len(failed_attempts),
            "eligible_candidates": len(eligible),
        },
        "time": {
            "successful_lifecycle_seconds": successful_seconds,
            "retained_failed_infrastructure_seconds": failed_seconds,
            "total_lifecycle_seconds": successful_seconds + failed_seconds,
        },
        "selection_rule": payload["recommended_selection_rule"],
        "inference_limits": {
            "independent_blocks": 4,
            "formal_superiority_claim_authorized": False,
            "wave_b_authorized": False,
            "apply_best_authorized": False,
        },
    }


def analyze_f4(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    block = _f4_block(settings, campaign_id)
    if block["status"] == "ANALYZED":
        analysis = dict(block["analysis"] or {})
        if _canonical_sha256(analysis) != str(block["analysis_sha256"]):
            raise ValueError("F4 durable analysis hash mismatch")
        return {**analysis, "analysis_sha256": str(block["analysis_sha256"])}
    rows, failed_attempts = _load_authenticated_rows(settings, campaign_id)
    _, payload = f4_manifest_payload()
    analysis = analyze_f4_observations(rows, failed_attempts, payload)
    analysis_sha256 = _canonical_sha256(analysis)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_f4_blocks
               SET status='ANALYZED',analysis=%s,analysis_sha256=%s,completed_at=clock_timestamp()
               WHERE f4_block_id=%s AND status='OBSERVATIONS_COMPLETE'""",
            (Jsonb(analysis), analysis_sha256, block["f4_block_id"]),
        )
        if cur.rowcount != 1:
            raise RuntimeError("F4 block lost its observations-complete state")
        conn.commit()
    control_campaign(
        settings,
        campaign_id,
        "stop",
        f"F4 analysis complete: {analysis['decision']}",
        actor=F4_STAGE,
    )
    return {**analysis, "analysis_sha256": analysis_sha256}


def export_f4_analysis(
    settings: Settings, campaign_id: uuid.UUID, output_path: Path | None = None
) -> dict[str, Any]:
    block = _f4_block(settings, campaign_id)
    if block["status"] != "ANALYZED" or block["analysis"] is None:
        raise ValueError("F4 export requires a terminal analyzed block")
    root = (settings.artifact_dir / F4_STAGE).resolve()
    output = (output_path or root / "f4-analysis.json").resolve()
    if output != root and root not in output.parents:
        raise ValueError("F4 export must stay under its artifact root")
    payload = {
        "campaign_id": str(campaign_id),
        "f4_block_id": str(block["f4_block_id"]),
        "manifest_sha256": str(block["manifest_sha256"]),
        "analysis_sha256": str(block["analysis_sha256"]),
        "analysis": dict(block["analysis"]),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return {
        "output_path": str(output),
        "file_sha256": _file_sha256(output),
        "analysis_sha256": str(block["analysis_sha256"]),
    }


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def render_f4_report(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    block = _f4_block(settings, campaign_id)
    if block["status"] != "ANALYZED" or block["analysis"] is None:
        raise ValueError("F4 report requires a terminal analyzed block")
    analysis = dict(block["analysis"])
    root = (settings.artifact_dir / F4_STAGE / "report").resolve()
    root.mkdir(parents=True, exist_ok=True)
    observations = list(analysis["observations"])
    summaries = []
    for row in analysis["candidate_summaries"]:
        summaries.append(
            {
                "treatment": row["treatment"],
                "source_primary_run_id": row["source_primary_run_id"],
                "mean_tps": row["raw_throughput_tps"]["mean"],
                "mean_p99_ms": row["raw_p99_ms"]["mean"],
                "mean_control_relative_tps_percent": row["control_relative_tps_percent"]["mean"],
                "mean_control_relative_p99_ms": row["control_relative_p99_ms"]["mean"],
                "eligible": row["eligible"],
            }
        )
    files: list[dict[str, Any]] = []
    content = {
        "tables/f4-observations.csv": _csv_bytes(observations),
        "tables/f4-candidate-summary.csv": _csv_bytes(summaries),
        "f4-report.md": (
            "# F4 confirmation report\n\n"
            f"Decision: **{analysis['decision']}**\n\n"
            f"Selected treatment: `{analysis['selected_treatment']}`\n\n"
            f"Valid observations: {analysis['counts']['valid_slots']}/20.\n\n"
            "Four restored common-seed blocks are the independent units. Results are repeated "
            "confirmation only; no formal superiority, Wave B, or apply-best claim is authorized.\n"
        ).encode(),
    }
    for relative, data in content.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files.append(
            {
                "relative_path": relative,
                "byte_size": len(data),
                "sha256": _file_sha256(path),
            }
        )
    index = {
        "campaign_id": str(campaign_id),
        "analysis_sha256": str(block["analysis_sha256"]),
        "files": sorted(files, key=lambda item: item["relative_path"]),
    }
    index_path = root / "f4-report-index.json"
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    return {
        "report_root": str(root),
        "index_path": str(index_path),
        "index_file_sha256": _file_sha256(index_path),
        "file_count": len(files) + 1,
    }


def f4_step_dict(step: F4Step) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(json.dumps(asdict(step), default=str)))
