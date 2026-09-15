from __future__ import annotations

import hashlib
import json
import platform
import socket
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Any

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.controller import (
    apply_configuration,
    discover_knobs,
    rollback_configuration,
    settings_equivalent,
    validate_candidate,
)
from charmdb.db import connect
from charmdb.metrics import capture_snapshot, numeric_difference
from charmdb.resources import (
    ResourceLimitError,
    capture_and_validate_resources,
    capture_runtime_resource_sample,
)
from charmdb.restore.candidate import (
    candidate_restore_result_dict,
    ensure_candidate_dataset_restored,
    verify_trial_candidate_restore,
)
from charmdb.workload import (
    PGBENCH_MAINTENANCE_POLICY,
    MeasurementExecution,
    load_measurement_marker,
    run_pgbench_measurement,
    run_pgbench_promoted_measurement,
    run_pgbench_warmup,
)

V2_BENCHMARK_WORKFLOWS = frozenset({"V2_BASELINE_BENCHMARK", "V2_TUNED_BENCHMARK"})
TUNED_BENCHMARK_WORKFLOWS = frozenset({"TUNED_BENCHMARK", "V2_TUNED_BENCHMARK"})
SATURATION_PHASE1_WORKFLOW = "V2_SATURATION_PHASE1"
TUNED_PRE_MEASUREMENT_RETRY_STATES = frozenset(
    {
        "RELOADING_OR_RESTARTING",
        "VERIFYING_DATABASE_HEALTH",
        "VERIFYING_ACTIVE_CONFIGURATION",
    }
)

ACTIVE_STATES = frozenset(
    {
        "CREATED",
        "CAPTURING_WORKLOAD_CONTEXT",
        "VALIDATING_ACTIONS",
        "ESTIMATING_STATIC_RISK",
        "RESTORING_CANDIDATE_DATASET",
        "VERIFYING_CANDIDATE_BASELINE",
        "APPLYING_KNOBS",
        "BUILDING_INDEXES",
        "RELOADING_OR_RESTARTING",
        "VERIFYING_DATABASE_HEALTH",
        "VERIFYING_ACTIVE_CONFIGURATION",
        "WARMING_UP",
        "RESETTING_OR_SNAPSHOTTING_COUNTERS",
        "RUNNING_PROXY_EVALUATION",
        "RUNNING_REDUCED_EVALUATION",
        "ASSESSING_PROMOTION",
        "RUNNING_FULL_EVALUATION",
        "COLLECTING_METRICS",
        "VALIDATING_MEASUREMENT",
        "CALCULATING_OBJECTIVES",
        "CALCULATING_CONSTRAINTS",
        "CALIBRATING_UNCERTAINTY",
        "PERSISTING_OBSERVATION",
        "UPDATING_SURROGATE",
        "UPDATING_DRIFT_MODEL",
        "SELECTING_NEXT_ACTION",
    }
)

TERMINAL_STATES = frozenset(
    {
        "COMPLETED",
        "STATICALLY_INFEASIBLE",
        "PREDICTED_UNSAFE",
        "INVALID_CONFIGURATION",
        "APPLY_FAILED",
        "INDEX_BUILD_FAILED",
        "INDEX_DROP_FAILED",
        "INDEX_BUDGET_EXCEEDED",
        "INDEX_NOT_USED",
        "RESTART_FAILED",
        "DATABASE_UNHEALTHY",
        "WORKLOAD_FAILED",
        "MEASUREMENT_INVALID",
        "SLO_VIOLATED",
        "RESOURCE_LIMIT_EXCEEDED",
        "DATASET_RESTORE_FAILED",
        "BASELINE_FINGERPRINT_FAILED",
        "LOW_FIDELITY_REJECTED",
        "EARLY_STOPPED",
        "CALIBRATION_INVALID",
        "TIMED_OUT",
        "ROLLED_BACK",
        "CANCELLED",
    }
)

HEALTH_TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"VERIFYING_DATABASE_HEALTH", "CANCELLED"}),
    "VERIFYING_DATABASE_HEALTH": frozenset(
        {"PERSISTING_OBSERVATION", "DATABASE_UNHEALTHY", "TIMED_OUT", "CANCELLED"}
    ),
    "PERSISTING_OBSERVATION": frozenset({"COMPLETED", "CANCELLED"}),
}

BENCHMARK_TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"CAPTURING_WORKLOAD_CONTEXT", "CANCELLED"}),
    "CAPTURING_WORKLOAD_CONTEXT": frozenset({"VALIDATING_ACTIONS", "CANCELLED"}),
    "VALIDATING_ACTIONS": frozenset(
        {
            "ESTIMATING_STATIC_RISK",
            "INVALID_CONFIGURATION",
            "RESOURCE_LIMIT_EXCEEDED",
            "CANCELLED",
        }
    ),
    "ESTIMATING_STATIC_RISK": frozenset(
        {"VERIFYING_DATABASE_HEALTH", "STATICALLY_INFEASIBLE", "CANCELLED"}
    ),
    "VERIFYING_DATABASE_HEALTH": frozenset(
        {"VERIFYING_ACTIVE_CONFIGURATION", "DATABASE_UNHEALTHY", "CANCELLED"}
    ),
    "VERIFYING_ACTIVE_CONFIGURATION": frozenset(
        {"WARMING_UP", "INVALID_CONFIGURATION", "CANCELLED"}
    ),
    "WARMING_UP": frozenset({"RESETTING_OR_SNAPSHOTTING_COUNTERS", "WORKLOAD_FAILED"}),
    "RESETTING_OR_SNAPSHOTTING_COUNTERS": frozenset(
        {"RUNNING_FULL_EVALUATION", "MEASUREMENT_INVALID"}
    ),
    "RUNNING_FULL_EVALUATION": frozenset({"COLLECTING_METRICS", "WORKLOAD_FAILED", "TIMED_OUT"}),
    "COLLECTING_METRICS": frozenset({"VALIDATING_MEASUREMENT", "MEASUREMENT_INVALID"}),
    "VALIDATING_MEASUREMENT": frozenset(
        {"CALCULATING_OBJECTIVES", "MEASUREMENT_INVALID", "RESOURCE_LIMIT_EXCEEDED"}
    ),
    "CALCULATING_OBJECTIVES": frozenset({"CALCULATING_CONSTRAINTS"}),
    "CALCULATING_CONSTRAINTS": frozenset({"PERSISTING_OBSERVATION", "SLO_VIOLATED"}),
    "PERSISTING_OBSERVATION": frozenset({"COMPLETED", "SLO_VIOLATED", "MEASUREMENT_INVALID"}),
}

TUNED_BENCHMARK_TRANSITIONS = dict(BENCHMARK_TRANSITIONS)
TUNED_BENCHMARK_TRANSITIONS.update(
    {
        "ESTIMATING_STATIC_RISK": frozenset(
            {"APPLYING_KNOBS", "STATICALLY_INFEASIBLE", "CANCELLED"}
        ),
        "APPLYING_KNOBS": frozenset({"RELOADING_OR_RESTARTING", "APPLY_FAILED"}),
        "RELOADING_OR_RESTARTING": frozenset({"VERIFYING_DATABASE_HEALTH", "RESTART_FAILED"}),
        "PERSISTING_OBSERVATION": frozenset({"SELECTING_NEXT_ACTION", "MEASUREMENT_INVALID"}),
        "SELECTING_NEXT_ACTION": frozenset({"COMPLETED", "ROLLED_BACK"}),
    }
)

V2_BENCHMARK_TRANSITIONS = dict(BENCHMARK_TRANSITIONS)
V2_BENCHMARK_TRANSITIONS.update(
    {
        "CREATED": frozenset({"RESTORING_CANDIDATE_DATASET", "CANCELLED"}),
        "RESTORING_CANDIDATE_DATASET": frozenset(
            {"VERIFYING_CANDIDATE_BASELINE", "DATASET_RESTORE_FAILED", "CANCELLED"}
        ),
        "VERIFYING_CANDIDATE_BASELINE": frozenset(
            {"CAPTURING_WORKLOAD_CONTEXT", "BASELINE_FINGERPRINT_FAILED", "CANCELLED"}
        ),
    }
)

V2_TUNED_BENCHMARK_TRANSITIONS = dict(TUNED_BENCHMARK_TRANSITIONS)
V2_TUNED_BENCHMARK_TRANSITIONS.update(
    {
        "CREATED": frozenset({"RESTORING_CANDIDATE_DATASET", "CANCELLED"}),
        "RESTORING_CANDIDATE_DATASET": frozenset(
            {"VERIFYING_CANDIDATE_BASELINE", "DATASET_RESTORE_FAILED", "CANCELLED"}
        ),
        "VERIFYING_CANDIDATE_BASELINE": frozenset(
            {"CAPTURING_WORKLOAD_CONTEXT", "BASELINE_FINGERPRINT_FAILED", "CANCELLED"}
        ),
    }
)



@dataclass(frozen=True)
class TrialLease:
    trial_id: uuid.UUID
    campaign_id: uuid.UUID
    state: str
    workflow_kind: str
    payload: dict[str, Any]
    attempt_count: int
    max_attempts: int
    owner: str
    token: uuid.UUID
    expires_at: datetime
    lease_seconds: int


@dataclass(frozen=True)
class WorkerResult:
    claimed: bool
    trial_id: uuid.UUID | None
    state: str | None
    recovered_stale_lease: bool


@dataclass(frozen=True)
class WorkerServiceResult:
    owner: str
    processed_trials: int
    state_counts: dict[str, int]
    idle_polls: int
    elapsed_seconds: float
    shutdown_requested: bool
    exit_reason: str


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4().hex[:12]}"


def validate_transition(workflow_kind: str, current: str, target: str) -> None:
    if current in TERMINAL_STATES:
        raise ValueError(f"terminal state {current} cannot transition to {target}")
    if target not in ACTIVE_STATES | TERMINAL_STATES:
        raise ValueError(f"unknown trial state {target}")
    if workflow_kind == "HEALTH_CHECK":
        allowed = HEALTH_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise ValueError(f"invalid HEALTH_CHECK transition {current} -> {target}")
    if workflow_kind == "BASELINE_BENCHMARK":
        allowed = BENCHMARK_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise ValueError(f"invalid BASELINE_BENCHMARK transition {current} -> {target}")
    if workflow_kind == "TUNED_BENCHMARK":
        allowed = TUNED_BENCHMARK_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise ValueError(f"invalid TUNED_BENCHMARK transition {current} -> {target}")
    if workflow_kind == "V2_BASELINE_BENCHMARK":
        allowed = V2_BENCHMARK_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise ValueError(f"invalid V2_BASELINE_BENCHMARK transition {current} -> {target}")
    if workflow_kind == "V2_TUNED_BENCHMARK":
        allowed = V2_TUNED_BENCHMARK_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise ValueError(f"invalid V2_TUNED_BENCHMARK transition {current} -> {target}")
    if workflow_kind == SATURATION_PHASE1_WORKFLOW:
        allowed = BENCHMARK_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise ValueError(
                f"invalid {SATURATION_PHASE1_WORKFLOW} transition {current} -> {target}"
            )


def create_campaign(
    settings: Settings,
    name: str,
    mode: str,
    objective: dict[str, Any],
    constraints: dict[str, Any],
    failure_limit: int = 5,
    campaign_settings: dict[str, Any] | None = None,
    actor: str = "cli",
) -> uuid.UUID:
    if not name.strip():
        raise ValueError("campaign name cannot be empty")
    if failure_limit < 1:
        raise ValueError("failure_limit must be positive")
    campaign_id = uuid.uuid4()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.campaigns
            (campaign_id,name,mode,status,objective_definition,constraint_definition,
             settings,failure_limit)
            VALUES (%s,%s,%s,'CREATED',%s,%s,%s,%s)""",
            (
                campaign_id,
                name.strip(),
                mode,
                Jsonb(objective),
                Jsonb(constraints),
                Jsonb(campaign_settings or {}),
                failure_limit,
            ),
        )
        cur.execute(
            """INSERT INTO charm_control.campaign_events
            (campaign_id,event_type,previous_status,new_status,actor,reason)
            VALUES (%s,'CREATE',NULL,'CREATED',%s,'campaign created')""",
            (campaign_id, actor),
        )
        conn.commit()
    return campaign_id


def control_campaign(
    settings: Settings,
    campaign_id: uuid.UUID,
    action: str,
    reason: str,
    actor: str = "cli",
) -> dict[str, Any]:
    transitions = {
        "pause": ({"RUNNING"}, "PAUSED"),
        "resume": ({"CREATED", "PAUSED"}, "RUNNING"),
        "stop": ({"CREATED", "RUNNING", "PAUSED", "STOPPING"}, "STOPPED"),
        "emergency-stop": ({"CREATED", "RUNNING", "PAUSED", "STOPPING"}, "STOPPED"),
    }
    if action not in transitions:
        raise ValueError(f"unsupported campaign action {action}")
    allowed, target = transitions[action]
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT status,emergency_stop FROM charm_control.campaigns
               WHERE campaign_id=%s FOR UPDATE""",
            (campaign_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"unknown campaign {campaign_id}")
        current = str(row["status"])
        if current not in allowed:
            raise ValueError(f"cannot {action} campaign in state {current}")
        emergency = action == "emergency-stop"
        cur.execute(
            """UPDATE charm_control.campaigns
            SET status=%s,emergency_stop=%s,stopped_reason=%s,updated_at=clock_timestamp()
            WHERE campaign_id=%s""",
            (target, emergency or bool(row["emergency_stop"]), reason, campaign_id),
        )
        cur.execute(
            """INSERT INTO charm_control.campaign_events
            (campaign_id,event_type,previous_status,new_status,actor,reason)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            (campaign_id, action.upper().replace("-", "_"), current, target, actor, reason),
        )
        cancelled = 0
        if emergency:
            cur.execute(
                """SELECT trial_id,state FROM charm_control.trials
                WHERE campaign_id=%s AND completed_at IS NULL FOR UPDATE""",
                (campaign_id,),
            )
            active = cur.fetchall()
            for trial in active:
                cur.execute(
                    """INSERT INTO charm_control.trial_transitions
                    (trial_id,from_state,to_state,reason,details)
                    VALUES (%s,%s,'CANCELLED',%s,%s)""",
                    (
                        trial["trial_id"],
                        trial["state"],
                        reason,
                        Jsonb({"emergency_stop": True, "actor": actor}),
                    ),
                )
            cur.execute(
                """UPDATE charm_control.trials
                SET state='CANCELLED',failure_type='CANCELLED',completed_at=clock_timestamp(),
                    lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL
                WHERE campaign_id=%s AND completed_at IS NULL""",
                (campaign_id,),
            )
            cancelled = cur.rowcount or 0
        conn.commit()
    return {"campaign_id": str(campaign_id), "status": target, "cancelled_trials": cancelled}


def campaign_status(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT campaign_id,name,mode,status,failure_count,failure_limit,emergency_stop,
                      stopped_reason,created_at,updated_at
               FROM charm_control.campaigns WHERE campaign_id=%s""",
            (campaign_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"unknown campaign {campaign_id}")
        cur.execute(
            """SELECT state,count(*) AS count FROM charm_control.trials
               WHERE campaign_id=%s GROUP BY state ORDER BY state""",
            (campaign_id,),
        )
        counts = {str(item["state"]): int(item["count"]) for item in cur.fetchall()}
    result = dict(row)
    result["campaign_id"] = str(result["campaign_id"])
    result["trial_counts"] = counts
    return result


def campaign_trials(settings: Settings, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM charm_control.campaigns WHERE campaign_id=%s",
            (campaign_id,),
        )
        if cur.fetchone() is None:
            raise ValueError(f"unknown campaign {campaign_id}")
        cur.execute(
            """SELECT trial_id,state,workflow_kind,benchmark_profile,fidelity,random_seed,
                      attempt_count,max_attempts,failure_type,created_at,started_at,completed_at
               FROM charm_control.trials WHERE campaign_id=%s ORDER BY created_at""",
            (campaign_id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        row["trial_id"] = str(row["trial_id"])
    return rows


def create_health_trial(
    settings: Settings,
    campaign_id: uuid.UUID,
    seed: int,
    idempotency_key: str,
    max_attempts: int = 3,
) -> uuid.UUID:
    if not idempotency_key.strip():
        raise ValueError("idempotency_key cannot be empty")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    trial_id = uuid.uuid4()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status,emergency_stop FROM charm_control.campaigns WHERE campaign_id=%s",
            (campaign_id,),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise ValueError(f"unknown campaign {campaign_id}")
        if campaign["status"] not in {"CREATED", "RUNNING", "PAUSED"}:
            raise ValueError(f"campaign does not accept trials in state {campaign['status']}")
        if campaign["emergency_stop"]:
            raise ValueError("campaign emergency stop is active")
        cur.execute(
            """INSERT INTO charm_control.trials
            (trial_id,campaign_id,state,benchmark_profile,fidelity,random_seed,
             workflow_kind,workflow_payload,max_attempts,idempotency_key)
            VALUES (%s,%s,'CREATED','monitor-health',0,%s,'HEALTH_CHECK',%s,%s,%s)
            ON CONFLICT (campaign_id,idempotency_key) WHERE idempotency_key IS NOT NULL
            DO NOTHING RETURNING trial_id""",
            (
                trial_id,
                campaign_id,
                seed,
                Jsonb({"target_host": settings.target_host, "target_db": settings.target_db}),
                max_attempts,
                idempotency_key,
            ),
        )
        inserted = cur.fetchone()
        if inserted is None:
            cur.execute(
                """SELECT trial_id FROM charm_control.trials
                   WHERE campaign_id=%s AND idempotency_key=%s""",
                (campaign_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is None:
                raise RuntimeError("idempotent trial lookup failed")
            conn.commit()
            return existing["trial_id"]  # type: ignore[no-any-return]
        cur.execute(
            """INSERT INTO charm_control.trial_transitions
            (trial_id,from_state,to_state,reason,details)
            VALUES (%s,NULL,'CREATED','health trial created',%s)""",
            (trial_id, Jsonb({"idempotency_key": idempotency_key})),
        )
        conn.commit()
    return trial_id


def create_baseline_benchmark_trial(
    settings: Settings,
    campaign_id: uuid.UUID,
    seed: int,
    idempotency_key: str,
    warmup_seconds: int = 2,
    duration_seconds: int = 5,
    concurrency: int = 4,
    fidelity: int = 3,
    p99_slo_ms: float = 20.0,
    max_attempts: int = 3,
    *,
    preflight_id: uuid.UUID | None = None,
    evidence_role: str | None = None,
    evaluation_role: str | None = None,
    restore_mechanism: str = "logical-restore",
    physical_archive_id: uuid.UUID | None = None,
) -> uuid.UUID:
    if not idempotency_key.strip():
        raise ValueError("idempotency_key cannot be empty")
    if warmup_seconds < 0 or duration_seconds < 1 or concurrency < 1:
        raise ValueError("invalid benchmark duration or concurrency")
    if fidelity not in {2, 3, 4}:
        raise ValueError("executed benchmark fidelity must be F2, F3, or F4")
    if p99_slo_ms <= 0 or max_attempts < 1:
        raise ValueError("p99 SLO and max_attempts must be positive")
    is_v2 = preflight_id is not None or evidence_role is not None or evaluation_role is not None
    if is_v2 and (preflight_id is None or evidence_role is None):
        raise ValueError("v2 baseline trials require preflight_id and evidence_role")
    if evidence_role not in {
        None,
        "INFRASTRUCTURE",
        "CALIBRATION",
        "PRIMARY",
        "SECONDARY",
        "F4_CONFIRMATION",
    }:
        raise ValueError("invalid evidence role for an executed v2 benchmark")
    if restore_mechanism not in {"logical-restore", "physical-archive"}:
        raise ValueError("unsupported v2 candidate restore mechanism")
    if (restore_mechanism == "physical-archive") != (physical_archive_id is not None):
        raise ValueError("physical-archive trials require exactly one physical_archive_id")
    metadata = discover_knobs(
        settings,
        {"random_page_cost", "work_mem", "effective_io_concurrency", "shared_buffers"},
    )
    expected = {str(row["name"]): str(row["boot_val"]) for row in metadata}
    active = {str(row["name"]): str(row["setting"]) for row in metadata}
    if active != expected:
        raise ValueError(
            f"baseline benchmark requires boot defaults: expected={expected}, active={active}"
        )
    trial_id = uuid.uuid4()
    profile = "durable-smoke" if duration_seconds <= 10 else "durable-development"
    payload = {
        "warmup_seconds": warmup_seconds,
        "duration_seconds": duration_seconds,
        "concurrency": concurrency,
        "pgbench_maintenance_policy": PGBENCH_MAINTENANCE_POLICY,
        "seed": seed,
        "p99_slo_ms": p99_slo_ms,
        "expected_configuration": expected,
        "label": "postgresql-default",
    }
    if preflight_id is not None:
        payload["preflight_id"] = str(preflight_id)
        payload["restore_mechanism"] = restore_mechanism
        if physical_archive_id is not None:
            payload["physical_archive_id"] = str(physical_archive_id)
    workflow_kind = "V2_BASELINE_BENCHMARK" if is_v2 else "BASELINE_BENCHMARK"
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status,emergency_stop FROM charm_control.campaigns WHERE campaign_id=%s",
            (campaign_id,),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise ValueError(f"unknown campaign {campaign_id}")
        if campaign["status"] not in {"CREATED", "RUNNING", "PAUSED"}:
            raise ValueError(f"campaign does not accept trials in state {campaign['status']}")
        if campaign["emergency_stop"]:
            raise ValueError("campaign emergency stop is active")
        cur.execute(
            """INSERT INTO charm_control.trials
            (trial_id,campaign_id,state,benchmark_profile,fidelity,random_seed,
             requested_configuration,workflow_kind,workflow_payload,max_attempts,idempotency_key,
             protocol_id,evidence_role,evaluation_role,candidate_restore_required)
            VALUES (%s,%s,'CREATED',%s,%s,%s,'{}'::jsonb,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (campaign_id,idempotency_key) WHERE idempotency_key IS NOT NULL
            DO NOTHING RETURNING trial_id""",
            (
                trial_id,
                campaign_id,
                profile,
                fidelity,
                seed,
                workflow_kind,
                Jsonb(payload),
                max_attempts,
                idempotency_key,
                "thesis-protocol-v2" if is_v2 else None,
                evidence_role,
                evaluation_role,
                is_v2,
            ),
        )
        inserted = cur.fetchone()
        if inserted is None:
            cur.execute(
                """SELECT trial_id FROM charm_control.trials
                   WHERE campaign_id=%s AND idempotency_key=%s""",
                (campaign_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is None:
                raise RuntimeError("idempotent benchmark trial lookup failed")
            conn.commit()
            return existing["trial_id"]  # type: ignore[no-any-return]
        cur.execute(
            """INSERT INTO charm_control.trial_transitions
            (trial_id,from_state,to_state,reason,details)
            VALUES (%s,NULL,'CREATED',%s,%s)""",
            (
                trial_id,
                "protocol-v2 baseline benchmark created"
                if is_v2
                else "durable baseline benchmark created",
                Jsonb(
                    {
                        "idempotency_key": idempotency_key,
                        "profile": profile,
                        "preflight_id": str(preflight_id) if preflight_id else None,
                        "evidence_role": evidence_role,
                    }
                ),
            ),
        )
        conn.commit()
    return trial_id


def create_v2_baseline_benchmark_trial(
    settings: Settings,
    campaign_id: uuid.UUID,
    preflight_id: uuid.UUID,
    evidence_role: str,
    seed: int,
    idempotency_key: str,
    warmup_seconds: int = 2,
    duration_seconds: int = 5,
    concurrency: int = 4,
    fidelity: int = 3,
    p99_slo_ms: float = 20.0,
    max_attempts: int = 3,
    evaluation_role: str = "DEFAULT_CONTROL",
    restore_mechanism: str = "logical-restore",
    physical_archive_id: uuid.UUID | None = None,
) -> uuid.UUID:
    return create_baseline_benchmark_trial(
        settings,
        campaign_id,
        seed,
        idempotency_key,
        warmup_seconds,
        duration_seconds,
        concurrency,
        fidelity,
        p99_slo_ms,
        max_attempts,
        preflight_id=preflight_id,
        evidence_role=evidence_role,
        evaluation_role=evaluation_role,
        restore_mechanism=restore_mechanism,
        physical_archive_id=physical_archive_id,
    )


def create_tuned_benchmark_trial(
    settings: Settings,
    campaign_id: uuid.UUID,
    candidate: dict[str, str],
    seed: int,
    idempotency_key: str,
    warmup_seconds: int = 2,
    duration_seconds: int = 5,
    concurrency: int = 4,
    p99_slo_ms: float = 20.0,
    max_attempts: int = 3,
) -> uuid.UUID:
    if not idempotency_key.strip():
        raise ValueError("idempotency_key cannot be empty")
    if not candidate or len(candidate) > 3:
        raise ValueError("initial durable tuned trial requires one to three knobs")
    requested = {str(name): str(value) for name, value in candidate.items()}
    metadata = discover_knobs(settings, set(requested))
    validate_candidate(requested, metadata)
    requires_restart = any(row["context"] == "postmaster" for row in metadata)
    initial = {str(row["name"]): str(row["setting"]) for row in metadata}
    boot = {str(row["name"]): str(row["boot_val"]) for row in metadata}
    if initial != boot:
        raise ValueError(
            f"tuned trial must start at boot defaults: expected={boot}, active={initial}"
        )
    if warmup_seconds < 0 or duration_seconds < 1 or concurrency < 1:
        raise ValueError("invalid benchmark duration or concurrency")
    if p99_slo_ms <= 0 or max_attempts < 1:
        raise ValueError("p99 SLO and max_attempts must be positive")
    trial_id = uuid.uuid4()
    payload = {
        "warmup_seconds": warmup_seconds,
        "duration_seconds": duration_seconds,
        "concurrency": concurrency,
        "pgbench_maintenance_policy": PGBENCH_MAINTENANCE_POLICY,
        "seed": seed,
        "p99_slo_ms": p99_slo_ms,
        "expected_configuration": requested,
        "previous_configuration": initial,
        "requested_configuration": requested,
        "requires_restart": requires_restart,
        "activation_class": "restart" if requires_restart else "reload",
        "label": "restart-knob-candidate" if requires_restart else "reload-knob-candidate",
    }
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status,emergency_stop FROM charm_control.campaigns WHERE campaign_id=%s",
            (campaign_id,),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise ValueError(f"unknown campaign {campaign_id}")
        if campaign["status"] not in {"CREATED", "RUNNING", "PAUSED"}:
            raise ValueError(f"campaign does not accept trials in state {campaign['status']}")
        if campaign["emergency_stop"]:
            raise ValueError("campaign emergency stop is active")
        cur.execute(
            """INSERT INTO charm_control.trials
            (trial_id,campaign_id,state,benchmark_profile,fidelity,random_seed,
             requested_configuration,workflow_kind,workflow_payload,max_attempts,idempotency_key)
            VALUES (%s,%s,'CREATED','durable-tuned-smoke',3,%s,%s,
                    'TUNED_BENCHMARK',%s,%s,%s)
            ON CONFLICT (campaign_id,idempotency_key) WHERE idempotency_key IS NOT NULL
            DO NOTHING RETURNING trial_id""",
            (
                trial_id,
                campaign_id,
                seed,
                Jsonb(requested),
                Jsonb(payload),
                max_attempts,
                idempotency_key,
            ),
        )
        inserted = cur.fetchone()
        if inserted is None:
            cur.execute(
                """SELECT trial_id FROM charm_control.trials
                   WHERE campaign_id=%s AND idempotency_key=%s""",
                (campaign_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is None:
                raise RuntimeError("idempotent tuned trial lookup failed")
            conn.commit()
            return existing["trial_id"]  # type: ignore[no-any-return]
        cur.execute(
            """INSERT INTO charm_control.trial_transitions
            (trial_id,from_state,to_state,reason,details)
            VALUES (%s,NULL,'CREATED','durable tuned benchmark created',%s)""",
            (
                trial_id,
                Jsonb({"idempotency_key": idempotency_key, "candidate": requested}),
            ),
        )
        conn.commit()
    return trial_id


def create_v2_tuned_benchmark_trial(
    settings: Settings,
    campaign_id: uuid.UUID,
    preflight_id: uuid.UUID,
    candidate: dict[str, str],
    seed: int,
    idempotency_key: str,
    *,
    evidence_role: str = "CALIBRATION",
    evaluation_role: str = "SATURATION_TRADEOFF",
    warmup_seconds: int = 120,
    duration_seconds: int = 600,
    concurrency: int = 32,
    client_threads: int = 4,
    max_attempts: int = 3,
    restore_mechanism: str = "logical-restore",
    physical_archive_id: uuid.UUID | None = None,
    benchmark_profile: str = "v2-saturation-phase2",
    runtime_samples_required: bool = False,
    promotion_rule: dict[str, Any] | None = None,
) -> uuid.UUID:
    if not idempotency_key.strip():
        raise ValueError("idempotency_key cannot be empty")
    if not 1 <= len(candidate) <= 12:
        raise ValueError("v2 tuned trials require one to twelve knobs")
    if evidence_role not in {
        "INFRASTRUCTURE",
        "CALIBRATION",
        "PRIMARY",
        "SECONDARY",
        "F4_CONFIRMATION",
    }:
        raise ValueError("invalid evidence role for an executed v2 benchmark")
    if warmup_seconds < 0 or duration_seconds < 1 or concurrency < 1:
        raise ValueError("invalid benchmark duration or concurrency")
    if not 1 <= client_threads <= concurrency:
        raise ValueError("client threads must be between one and concurrency")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if not benchmark_profile.strip():
        raise ValueError("benchmark profile cannot be empty")
    if restore_mechanism not in {"logical-restore", "physical-archive"}:
        raise ValueError("unsupported v2 candidate restore mechanism")
    if (restore_mechanism == "physical-archive") != (physical_archive_id is not None):
        raise ValueError("physical-archive trials require exactly one physical_archive_id")
    if promotion_rule is not None:
        required_promotion = {
            "prefix_seconds",
            "continuation_seconds",
            "baseline_throughput_tps",
            "throughput_floor_ratio",
            "continuation_seed",
        }
        if set(promotion_rule) != required_promotion:
            raise ValueError("promotion rule fields differ from the durable two-stage contract")
        if (
            int(promotion_rule["prefix_seconds"]) < 1
            or int(promotion_rule["continuation_seconds"]) < 1
            or float(promotion_rule["baseline_throughput_tps"]) <= 0
            or not 0 < float(promotion_rule["throughput_floor_ratio"]) <= 1
            or int(promotion_rule["continuation_seed"]) < 1
        ):
            raise ValueError("invalid durable two-stage promotion rule")
        expected = int(promotion_rule["prefix_seconds"]) + int(
            promotion_rule["continuation_seconds"]
        )
        if duration_seconds != expected:
            raise ValueError("tuned duration must equal prefix plus continuation duration")

    requested = {str(name): str(value) for name, value in candidate.items()}
    metadata = discover_knobs(settings, set(requested))
    validate_candidate(requested, metadata)
    boot = {str(row["name"]): str(row["boot_val"]) for row in metadata}
    discovered_restart = any(row["context"] == "postmaster" for row in metadata)
    trial_id = uuid.uuid4()
    payload = {
        "preflight_id": str(preflight_id),
        "restore_mechanism": restore_mechanism,
        "warmup_seconds": warmup_seconds,
        "duration_seconds": duration_seconds,
        "concurrency": concurrency,
        "client_threads": client_threads,
        "pgbench_maintenance_policy": PGBENCH_MAINTENANCE_POLICY,
        "seed": seed,
        "p99_slo_ms": 1_000_000_000.0,
        "p99_slo_applied": False,
        "expected_configuration": requested,
        "previous_configuration": boot,
        "requested_configuration": requested,
        "requires_restart": discovered_restart,
        "unconditional_restart": True,
        "activation_class": "unconditional-restart",
        "runtime_samples_required": runtime_samples_required,
        "label": evaluation_role.lower().replace("_", "-"),
    }
    if physical_archive_id is not None:
        payload["physical_archive_id"] = str(physical_archive_id)
    if promotion_rule is not None:
        payload["promotion_rule"] = dict(promotion_rule)

    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status,emergency_stop FROM charm_control.campaigns WHERE campaign_id=%s",
            (campaign_id,),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise ValueError(f"unknown campaign {campaign_id}")
        if campaign["status"] not in {"CREATED", "RUNNING", "PAUSED"}:
            raise ValueError(f"campaign does not accept trials in state {campaign['status']}")
        if campaign["emergency_stop"]:
            raise ValueError("campaign emergency stop is active")
        cur.execute(
            """INSERT INTO charm_control.trials
            (trial_id,campaign_id,state,benchmark_profile,fidelity,random_seed,
             requested_configuration,workflow_kind,workflow_payload,max_attempts,idempotency_key,
             protocol_id,evidence_role,evaluation_role,candidate_restore_required)
            VALUES (%s,%s,'CREATED',%s,3,%s,%s,
                    'V2_TUNED_BENCHMARK',%s,%s,%s,'thesis-protocol-v2',%s,%s,true)
            ON CONFLICT (campaign_id,idempotency_key) WHERE idempotency_key IS NOT NULL
            DO NOTHING RETURNING trial_id""",
            (
                trial_id,
                campaign_id,
                benchmark_profile,
                seed,
                Jsonb(requested),
                Jsonb(payload),
                max_attempts,
                idempotency_key,
                evidence_role,
                evaluation_role,
            ),
        )
        inserted = cur.fetchone()
        if inserted is None:
            cur.execute(
                """SELECT trial_id FROM charm_control.trials
                   WHERE campaign_id=%s AND idempotency_key=%s""",
                (campaign_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is None:
                raise RuntimeError("idempotent v2 tuned trial lookup failed")
            conn.commit()
            return existing["trial_id"]  # type: ignore[no-any-return]
        cur.execute(
            """INSERT INTO charm_control.trial_transitions
            (trial_id,from_state,to_state,reason,details)
            VALUES (%s,NULL,'CREATED','protocol-v2 tuned benchmark created',%s)""",
            (
                trial_id,
                Jsonb(
                    {
                        "idempotency_key": idempotency_key,
                        "preflight_id": str(preflight_id),
                        "evidence_role": evidence_role,
                        "evaluation_role": evaluation_role,
                        "unconditional_restart": True,
                    }
                ),
            ),
        )
        conn.commit()
    return trial_id




def claim_next_trial(
    settings: Settings,
    owner: str,
    lease_seconds: int = 30,
    campaign_id: uuid.UUID | None = None,
) -> tuple[TrialLease | None, bool]:
    if lease_seconds < 5:
        raise ValueError("lease_seconds must be at least five")
    token = uuid.uuid4()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """WITH candidate AS (
                SELECT t.trial_id,
                       (t.lease_expires_at IS NOT NULL AND t.lease_expires_at <= clock_timestamp())
                           AS stale
                FROM charm_control.trials t
                JOIN charm_control.campaigns c USING(campaign_id)
                WHERE c.status='RUNNING' AND NOT c.emergency_stop
                  AND (%s::uuid IS NULL OR t.campaign_id=%s)
                  AND t.completed_at IS NULL
                  AND t.attempt_count < t.max_attempts
                  AND t.next_attempt_at <= clock_timestamp()
                  AND (t.lease_expires_at IS NULL OR t.lease_expires_at <= clock_timestamp())
                ORDER BY t.created_at
                FOR UPDATE OF t SKIP LOCKED
                LIMIT 1
            )
            UPDATE charm_control.trials t
            SET lease_owner=%s,lease_token=%s,lease_acquired_at=clock_timestamp(),
                heartbeat_at=clock_timestamp(),
                lease_expires_at=clock_timestamp() + (%s * interval '1 second'),
                attempt_count=t.attempt_count + 1,
                started_at=COALESCE(t.started_at,clock_timestamp())
            FROM candidate
            WHERE t.trial_id=candidate.trial_id
            RETURNING t.trial_id,t.campaign_id,t.state,t.workflow_kind,t.workflow_payload,
                      t.attempt_count,t.max_attempts,t.lease_expires_at,candidate.stale""",
            (campaign_id, campaign_id, owner, token, lease_seconds),
        )
        row = cur.fetchone()
        conn.commit()
    if row is None:
        return None, False
    lease = TrialLease(
        trial_id=row["trial_id"],
        campaign_id=row["campaign_id"],
        state=str(row["state"]),
        workflow_kind=str(row["workflow_kind"]),
        payload=dict(row["workflow_payload"]),
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        owner=owner,
        token=token,
        expires_at=row["lease_expires_at"],
        lease_seconds=lease_seconds,
    )
    return lease, bool(row["stale"])


class TrialLeaseLost(RuntimeError):
    """The lease is provably no longer held by this worker.

    Raised only when the heartbeat statement reached the control database and
    matched no row, meaning the lease expired or another owner took it. A
    failure to reach the control database is a transient error and must not be
    reported as a lost lease.
    """


def heartbeat(settings: Settings, lease: TrialLease, lease_seconds: int = 30) -> datetime:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.trials
            SET heartbeat_at=clock_timestamp(),
                lease_expires_at=clock_timestamp() + (%s * interval '1 second')
            WHERE trial_id=%s AND lease_owner=%s AND lease_token=%s
              AND lease_expires_at > clock_timestamp()
            RETURNING lease_expires_at""",
            (lease_seconds, lease.trial_id, lease.owner, lease.token),
        )
        row = cur.fetchone()
        if row is None:
            raise TrialLeaseLost("trial lease was lost before heartbeat")
        conn.commit()
        return row["lease_expires_at"]  # type: ignore[no-any-return]


def _advance(
    settings: Settings,
    lease: TrialLease,
    expected: str,
    target: str,
    reason: str,
    details: dict[str, Any] | None = None,
) -> None:
    validate_transition(lease.workflow_kind, expected, target)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT state FROM charm_control.trials
            WHERE trial_id=%s AND lease_owner=%s AND lease_token=%s
              AND lease_expires_at > clock_timestamp() FOR UPDATE""",
            (lease.trial_id, lease.owner, lease.token),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("trial lease is absent or expired")
        if row["state"] != expected:
            raise RuntimeError(f"expected state {expected}, found {row['state']}")
        cur.execute(
            """INSERT INTO charm_control.trial_transitions
            (trial_id,from_state,to_state,reason,details) VALUES (%s,%s,%s,%s,%s)""",
            (lease.trial_id, expected, target, reason, Jsonb(details or {})),
        )
        cur.execute(
            "UPDATE charm_control.trials SET state=%s WHERE trial_id=%s",
            (target, lease.trial_id),
        )
        conn.commit()


def _record_action_start(settings: Settings, lease: TrialLease, state: str) -> bool:
    key = f"{lease.trial_id}:{state}"
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.trial_action_executions
            (execution_id,trial_id,state,idempotency_key,status,attempt,lease_token)
            VALUES (%s,%s,%s,%s,'STARTED',%s,%s)
            ON CONFLICT (trial_id,state,idempotency_key) DO UPDATE
            SET status=CASE
                    WHEN charm_control.trial_action_executions.status='COMPLETED'
                    THEN 'COMPLETED' ELSE 'STARTED' END,
                attempt=CASE
                    WHEN charm_control.trial_action_executions.status='COMPLETED'
                    THEN charm_control.trial_action_executions.attempt ELSE EXCLUDED.attempt END,
                lease_token=CASE
                    WHEN charm_control.trial_action_executions.status='COMPLETED'
                    THEN charm_control.trial_action_executions.lease_token
                    ELSE EXCLUDED.lease_token END,
                started_at=CASE
                    WHEN charm_control.trial_action_executions.status='COMPLETED'
                    THEN charm_control.trial_action_executions.started_at
                    ELSE clock_timestamp() END,
                completed_at=CASE
                    WHEN charm_control.trial_action_executions.status='COMPLETED'
                    THEN charm_control.trial_action_executions.completed_at ELSE NULL END
            RETURNING status""",
            (uuid.uuid4(), lease.trial_id, state, key, lease.attempt_count, lease.token),
        )
        row = cur.fetchone()
        conn.commit()
    return row is not None and row["status"] == "COMPLETED"


def _record_action_complete(
    settings: Settings, lease: TrialLease, state: str, result: dict[str, Any]
) -> None:
    key = f"{lease.trial_id}:{state}"
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.trial_action_executions
            SET status='COMPLETED',result=%s,completed_at=clock_timestamp()
            WHERE trial_id=%s AND state=%s AND idempotency_key=%s AND lease_token=%s""",
            (Jsonb(result), lease.trial_id, state, key, lease.token),
        )
        if cur.rowcount != 1:
            raise RuntimeError("action completion lost its lease token")
        conn.commit()


def _completed_action_result(
    settings: Settings, trial_id: uuid.UUID, state: str
) -> dict[str, Any] | None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT result FROM charm_control.trial_action_executions
            WHERE trial_id=%s AND state=%s AND status='COMPLETED'""",
            (trial_id, state),
        )
        row = cur.fetchone()
    if row is None or row["result"] is None:
        return None
    return dict(row["result"])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _health_snapshot(settings: Settings) -> dict[str, Any]:
    settings.assert_target_allowed()
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT current_database() AS database,current_setting('server_version') AS version,
                      NOT pg_is_in_recovery() AS writable,
                      current_setting('shared_buffers') AS shared_buffers"""
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("target health query returned no row")
        return dict(row)


def _run_action(
    settings: Settings,
    lease: TrialLease,
    state: str,
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    if _record_action_start(settings, lease, state):
        completed = _completed_action_result(settings, lease.trial_id, state)
        if completed is None:
            raise RuntimeError(f"completed action {state} has no result")
        return completed

    # Target inspection, resource capture, configuration changes, and index work can
    # all outlive a short lease. Keep ownership current for the entire durable action,
    # rather than only while a pgbench subprocess is running.
    heartbeat(settings, lease, lease.lease_seconds)
    stop_heartbeat = Event()
    heartbeat_errors: list[Exception] = []
    absorbed: list[Exception] = []
    interval = max(0.1, min(5.0, lease.lease_seconds / 3))

    def renew_lease() -> None:
        """Keep the lease current, tolerating transient control-database errors.

        A single failed renewal must not destroy a multi-minute durable action.
        The lease only becomes unsafe once a full ``lease_seconds`` window has
        passed with no successful renewal, so that is the give-up rule. A
        provably lost lease still fails immediately, because continuing to work
        under someone else's claim would corrupt the ledger.
        """
        last_success = time.monotonic()
        while not stop_heartbeat.wait(interval):
            try:
                heartbeat(settings, lease, lease.lease_seconds)
            except TrialLeaseLost as error:
                heartbeat_errors.append(error)
                return
            except Exception as error:
                absorbed.append(error)
                if time.monotonic() - last_success >= lease.lease_seconds:
                    heartbeat_errors.append(error)
                    return
            else:
                last_success = time.monotonic()

    heartbeat_thread = Thread(
        target=renew_lease,
        name=f"trial-heartbeat-{lease.trial_id}",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        result = operation()
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join()
    if heartbeat_errors:
        cause = heartbeat_errors[0]
        raise RuntimeError(
            f"trial lease heartbeat failed during action {state} after "
            f"{len(absorbed)} transient error(s) within a "
            f"{lease.lease_seconds}-second lease window: "
            f"{type(cause).__name__}: {cause}"
        ) from cause
    _record_action_complete(settings, lease, state, result)
    return result


def _benchmark_output_dir(settings: Settings, lease: TrialLease) -> Path:
    return settings.artifact_dir / "raw" / str(lease.campaign_id) / str(lease.trial_id)


def _capture_benchmark_context(settings: Settings, lease: TrialLease) -> dict[str, Any]:
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT current_database() AS database,current_setting('server_version') AS version,
                      pg_database_size(current_database()) AS database_size_bytes,
                      current_setting('server_encoding') AS server_encoding"""
        )
        target = cur.fetchone()
        if target is None:
            raise RuntimeError("target context query returned no row")
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "target": dict(target),
        "client": {"python": platform.python_version(), "platform": platform.platform()},
        "workflow_payload": lease.payload,
    }


def _validate_benchmark_actions(settings: Settings, lease: TrialLease) -> dict[str, Any]:
    required = {
        "warmup_seconds",
        "duration_seconds",
        "concurrency",
        "seed",
        "p99_slo_ms",
        "expected_configuration",
    }
    missing = sorted(required - lease.payload.keys())
    if missing:
        raise ValueError(f"benchmark payload missing {missing}")
    if int(lease.payload["duration_seconds"]) < 1:
        raise ValueError("benchmark duration must be positive")
    if int(lease.payload["concurrency"]) < 1:
        raise ValueError("benchmark concurrency must be positive")
    expected = lease.payload["expected_configuration"]
    if not isinstance(expected, dict) or not expected:
        raise ValueError("expected baseline configuration is absent")
    mutation_count = 0
    if lease.workflow_kind in TUNED_BENCHMARK_WORKFLOWS:
        requested = {
            str(name): str(value)
            for name, value in dict(lease.payload["requested_configuration"]).items()
        }
        metadata = discover_knobs(settings, set(requested))
        validate_candidate(requested, metadata)
        discovered_restart = any(row["context"] == "postmaster" for row in metadata)
        declared_restart = bool(lease.payload.get("requires_restart", False))
        if discovered_restart != declared_restart:
            raise ValueError(
                "declared restart requirement does not match PostgreSQL setting metadata"
            )
        if lease.workflow_kind == "V2_TUNED_BENCHMARK" and not bool(
            lease.payload.get("unconditional_restart", False)
        ):
            raise ValueError("v2 tuned benchmarks require unconditional restart activation")
        mutation_count = len(requested)
    resources = capture_and_validate_resources(settings)
    return {
        "valid": True,
        "mutation_count": mutation_count,
        "requires_restart": bool(lease.payload.get("requires_restart", False)),
        "expected_configuration": expected,
        "resource_snapshot": resources,
    }


def _application_ids(trial_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    application_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{trial_id}:application")
    snapshot_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{trial_id}:snapshot")
    return application_id, snapshot_id


def _apply_tuned_configuration(settings: Settings, lease: TrialLease) -> dict[str, Any]:
    application_id, snapshot_id = _application_ids(lease.trial_id)
    requested = {
        str(name): str(value)
        for name, value in dict(lease.payload["requested_configuration"]).items()
    }
    result = apply_configuration(
        settings,
        requested,
        application_id=application_id,
        snapshot_id=snapshot_id,
        force_restart=bool(lease.payload.get("unconditional_restart", False)),
    )
    return {
        "application_id": str(result.application_id),
        "snapshot_id": str(result.snapshot_id),
        "requested": result.requested,
        "previous": result.previous,
        "verified": result.verified,
        "requires_restart": result.requires_restart,
        "duration_seconds": result.duration_seconds,
    }


def _verify_tuned_activation(settings: Settings, lease: TrialLease) -> dict[str, Any]:
    application_id, _snapshot_id = _application_ids(lease.trial_id)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT status,requires_restart,verified_settings,apply_duration_seconds
               FROM charm_control.configuration_applications WHERE application_id=%s""",
            (application_id,),
        )
        row = cur.fetchone()
    if row is None or row["status"] != "VERIFIED":
        raise RuntimeError("configuration application is not verified")
    requested = {
        str(name): str(value)
        for name, value in dict(lease.payload["requested_configuration"]).items()
    }
    metadata = discover_knobs(settings, set(requested))
    active = {str(item["name"]): str(item["setting"]) for item in metadata}
    pending = {str(item["name"]): bool(item["pending_restart"]) for item in metadata}
    requires_restart = bool(row["requires_restart"])
    if requires_restart != bool(lease.payload.get("requires_restart", False)):
        raise RuntimeError("stored restart requirement differs from the durable trial payload")
    if not settings_equivalent(requested, active, metadata):
        raise RuntimeError(f"active settings differ: requested={requested}, active={active}")
    if any(pending.values()):
        raise RuntimeError(f"configuration still has pending restart flags: {pending}")
    return {
        "application_id": str(application_id),
        "status": str(row["status"]),
        "requires_restart": requires_restart,
        "verified_settings": dict(row["verified_settings"]),
        "active_settings": active,
        "pending_restart": pending,
        "duration_seconds": float(row["apply_duration_seconds"] or 0.0),
    }


def _rollback_tuned_configuration(settings: Settings, lease: TrialLease) -> dict[str, Any]:
    application_id, _snapshot_id = _application_ids(lease.trial_id)
    started = time.monotonic()
    restored = rollback_configuration(
        settings,
        application_id,
        "durable tuned trial completed",
    )
    return {
        "application_id": str(application_id),
        "restored": restored,
        "rollback_duration_seconds": time.monotonic() - started,
    }


def _verify_baseline_configuration(settings: Settings, lease: TrialLease) -> dict[str, Any]:
    expected = {
        str(name): str(value)
        for name, value in dict(lease.payload["expected_configuration"]).items()
    }
    rows = discover_knobs(settings, set(expected))
    active = {str(row["name"]): str(row["setting"]) for row in rows}
    pending = {str(row["name"]): bool(row["pending_restart"]) for row in rows}
    if not settings_equivalent(expected, active, rows):
        raise ValueError(f"active configuration changed: expected={expected}, active={active}")
    if any(pending.values()):
        raise ValueError(f"baseline configuration has pending restart: {pending}")
    return {"active_configuration": active, "pending_restart": pending}


def _persist_metric_snapshot(
    settings: Settings,
    lease: TrialLease,
    phase: str,
) -> dict[str, Any]:
    with connect(settings.target_dsn) as target:
        snapshot = capture_snapshot(target)
        target.commit()
    with connect(settings.control_dsn) as control, control.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.metric_snapshots
            (snapshot_id,trial_id,phase,source,payload)
            VALUES (%s,%s,%s,'postgresql',%s)
            ON CONFLICT (trial_id,phase,source) DO UPDATE
            SET payload=EXCLUDED.payload,captured_at=clock_timestamp()""",
            (uuid.uuid4(), lease.trial_id, phase, Jsonb(snapshot)),
        )
        control.commit()
    return {"phase": phase, "snapshot": snapshot}


def _measurement_execution_from_result(
    settings: Settings, action_result: dict[str, Any]
) -> MeasurementExecution:
    relative = Path(str(action_result["marker_relative_path"]))
    marker = settings.artifact_dir / relative
    return load_measurement_marker(marker)


def _workload_application_name(lease: TrialLease, phase: str) -> str:
    name = f"charmdb:{lease.trial_id}:{phase}"
    if len(name) > 63:
        raise ValueError("workload application identity exceeds PostgreSQL limit")
    return name


def _terminate_orphan_workload_sessions(
    settings: Settings, application_name: str, timeout_seconds: float = 10.0
) -> dict[str, Any]:
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT pid FROM pg_stat_activity
               WHERE application_name=%s AND pid<>pg_backend_pid() ORDER BY pid""",
            (application_name,),
        )
        pids = [int(row["pid"]) for row in cur.fetchall()]
        terminated = 0
        for pid in pids:
            cur.execute("SELECT pg_terminate_backend(%s) AS terminated", (pid,))
            row = cur.fetchone()
            terminated += int(bool(row and row["terminated"]))
        conn.commit()
    deadline = time.monotonic() + timeout_seconds
    remaining = len(pids)
    while remaining and time.monotonic() < deadline:
        with connect(settings.target_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) AS count FROM pg_stat_activity
                   WHERE application_name=%s AND pid<>pg_backend_pid()""",
                (application_name,),
            )
            row = cur.fetchone()
            remaining = int(row["count"]) if row is not None else 0
        if remaining:
            time.sleep(0.1)
    if remaining:
        raise RuntimeError(
            f"orphan workload sessions did not exit for application_name={application_name}"
        )
    return {
        "application_name": application_name,
        "observed_pids": pids,
        "terminated_sessions": terminated,
        "remaining_sessions": remaining,
    }


def _execute_or_recover_measurement(
    settings: Settings,
    lease: TrialLease,
    lease_seconds: int,
) -> dict[str, Any]:
    duration = int(lease.payload["duration_seconds"])
    heartbeat(settings, lease, lease_seconds)
    poll_interval = max(1.0, min(5.0, lease_seconds / 3))
    output_dir = _benchmark_output_dir(settings, lease)
    markers = sorted(output_dir.glob("measurement-attempt-*.json"))
    if markers:
        execution = load_measurement_marker(markers[-1])
        recovered_marker = True
        orphan_cleanup = {
            "application_name": _workload_application_name(lease, "measurement"),
            "observed_pids": [],
            "terminated_sessions": 0,
            "remaining_sessions": 0,
            "skipped_for_completed_marker": True,
        }
    else:
        application_name = _workload_application_name(lease, "measurement")
        promotion_rule = lease.payload.get("promotion_rule")
        if promotion_rule is None:
            orphan_cleanup = _terminate_orphan_workload_sessions(settings, application_name)
        else:
            orphan_cleanup = {
                "stages": [
                    _terminate_orphan_workload_sessions(settings, f"{application_name}-f2"),
                    _terminate_orphan_workload_sessions(settings, f"{application_name}-f3"),
                ]
            }

        def renew_lease() -> None:
            heartbeat(settings, lease, lease_seconds)

        common = {
            "progress_callback": renew_lease,
            "poll_interval_seconds": poll_interval,
            "application_name": application_name,
            "client_threads": (
                int(lease.payload["client_threads"])
                if lease.payload.get("client_threads") is not None
                else None
            ),
            "runtime_sample_callback": (
                (lambda: capture_runtime_resource_sample(settings))
                if lease.workflow_kind == SATURATION_PHASE1_WORKFLOW
                or bool(lease.payload.get("runtime_samples_required"))
                else None
            ),
        }
        if promotion_rule is None:
            execution = run_pgbench_measurement(
                settings,
                output_dir,
                duration,
                int(lease.payload["concurrency"]),
                int(lease.payload["seed"]),
                lease.attempt_count,
                **common,
            )
        else:
            rule = dict(promotion_rule)
            execution = run_pgbench_promoted_measurement(
                settings,
                output_dir,
                int(rule["prefix_seconds"]),
                int(rule["continuation_seconds"]),
                int(lease.payload["concurrency"]),
                int(lease.payload["seed"]),
                int(rule["continuation_seed"]),
                lease.attempt_count,
                float(rule["baseline_throughput_tps"]),
                float(rule["throughput_floor_ratio"]),
                **common,
            )
        recovered_marker = False
    relative = execution.marker_path.relative_to(settings.artifact_dir)
    promotion = execution.runtime_telemetry.get("promotion")
    return {
        "result": asdict(execution.result),
        "marker_relative_path": str(relative),
        "marker_sha256": _sha256(execution.marker_path),
        "recovered_marker": recovered_marker,
        "command": execution.command,
        "runtime_telemetry": execution.runtime_telemetry,
        "orphan_cleanup": orphan_cleanup,
        "promotion": promotion,
    }


def _benchmark_action_result(settings: Settings, lease: TrialLease, state: str) -> dict[str, Any]:
    result = _completed_action_result(settings, lease.trial_id, state)
    if result is None:
        raise RuntimeError(f"required completed action {state} is absent")
    return result


def _validate_measurement(settings: Settings, lease: TrialLease) -> dict[str, Any]:
    action = _benchmark_action_result(settings, lease, "RUNNING_FULL_EVALUATION")
    execution = _measurement_execution_from_result(settings, action)
    result = execution.result
    expected_duration = int(lease.payload["duration_seconds"])
    promotion = action.get("promotion")
    if lease.payload.get("promotion_rule") is not None:
        if not isinstance(promotion, dict) or not isinstance(promotion.get("promoted"), bool):
            raise ValueError("two-stage measurement lacks an authenticated promotion decision")
        rule = dict(lease.payload["promotion_rule"])
        expected_duration = int(rule["prefix_seconds"]) + (
            int(rule["continuation_seconds"]) if promotion["promoted"] else 0
        )
    if result.transactions < 1 or result.latency_samples < 1:
        raise ValueError("measurement has no completed client observations")
    if result.duration_seconds < expected_duration * 0.8:
        raise ValueError(
            f"measurement ended early: {result.duration_seconds:.3f}s < 80% of {expected_duration}s"
        )
    before = _benchmark_action_result(settings, lease, "RESETTING_OR_SNAPSHOTTING_COUNTERS")[
        "snapshot"
    ]
    after = _benchmark_action_result(settings, lease, "COLLECTING_METRICS")["snapshot"]
    if before.get("server_version") != after.get("server_version"):
        raise ValueError("server version changed during measurement")
    resources = capture_and_validate_resources(settings)
    return {
        "valid": True,
        "transactions": result.transactions,
        "latency_samples": result.latency_samples,
        "metric_difference": numeric_difference(before, after),
        "resource_snapshot": resources,
    }


def _calculate_benchmark_objectives(settings: Settings, lease: TrialLease) -> dict[str, Any]:
    action = _benchmark_action_result(settings, lease, "RUNNING_FULL_EVALUATION")
    result = _measurement_execution_from_result(settings, action).result
    return {
        "throughput_tps": result.throughput_tps,
        "p50_ms": result.p50_ms,
        "p95_ms": result.p95_ms,
        "p99_ms": result.p99_ms,
    }


def _calculate_benchmark_constraints(settings: Settings, lease: TrialLease) -> dict[str, Any]:
    action = _benchmark_action_result(settings, lease, "RUNNING_FULL_EVALUATION")
    result = _measurement_execution_from_result(settings, action).result
    if lease.workflow_kind == SATURATION_PHASE1_WORKFLOW or lease.workflow_kind in {
        "V2_BASELINE_BENCHMARK",
        "V2_TUNED_BENCHMARK",
    }:
        return {
            "failures": result.failures,
            "failure_rate": result.failures / max(1, result.transactions + result.failures),
            "p99_is_objective": True,
            "p99_slo_applied": False,
            "feasible": result.failures == 0,
        }
    slo = float(lease.payload["p99_slo_ms"])
    return {
        "failures": result.failures,
        "failure_rate": result.failures / max(1, result.transactions + result.failures),
        "p99_slo_ms": slo,
        "p99_margin_ms": slo - result.p99_ms,
        "feasible": result.failures == 0 and result.p99_ms <= slo,
    }


def _persist_benchmark_observation(settings: Settings, lease: TrialLease) -> dict[str, Any]:
    context = _benchmark_action_result(settings, lease, "CAPTURING_WORKLOAD_CONTEXT")
    verification = _benchmark_action_result(settings, lease, "VERIFYING_ACTIVE_CONFIGURATION")
    measurement_action = _benchmark_action_result(settings, lease, "RUNNING_FULL_EVALUATION")
    execution = _measurement_execution_from_result(settings, measurement_action)
    before = _benchmark_action_result(settings, lease, "RESETTING_OR_SNAPSHOTTING_COUNTERS")[
        "snapshot"
    ]
    after = _benchmark_action_result(settings, lease, "COLLECTING_METRICS")["snapshot"]
    validation = _benchmark_action_result(settings, lease, "VALIDATING_MEASUREMENT")
    objectives = _benchmark_action_result(settings, lease, "CALCULATING_OBJECTIVES")
    constraints = _benchmark_action_result(settings, lease, "CALCULATING_CONSTRAINTS")
    payload = {
        "campaign_id": str(lease.campaign_id),
        "trial_id": str(lease.trial_id),
        "workflow_kind": lease.workflow_kind,
        "benchmark": lease.payload,
        "context": context,
        "active_configuration": verification["active_configuration"],
        "result": asdict(execution.result),
        "measurement_marker": measurement_action["marker_relative_path"],
        "runtime_telemetry": execution.runtime_telemetry,
        "metrics_before": before,
        "metrics_after": after,
        "metric_difference": numeric_difference(before, after),
        "validation": validation,
        "objectives": objectives,
        "constraints": constraints,
    }
    output_dir = _benchmark_output_dir(settings, lease)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / "trial.json"
    temporary = artifact.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temporary.replace(artifact)
    relative = artifact.relative_to(settings.artifact_dir)
    digest = _sha256(artifact)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.trials
            SET active_configuration=%s,objective_values=%s,constraint_values=%s,
                software_versions=%s,host_snapshot=%s,workflow_result=%s
            WHERE trial_id=%s""",
            (
                Jsonb(verification["active_configuration"]),
                Jsonb(objectives),
                Jsonb(constraints),
                Jsonb(
                    {
                        "postgresql": context["target"]["version"],
                        "python": platform.python_version(),
                        "charmdb": "0.1.0",
                    }
                ),
                Jsonb(context["client"]),
                Jsonb(
                    {
                        **payload["result"],
                        "measurement_marker": payload["measurement_marker"],
                        "runtime_telemetry": execution.runtime_telemetry,
                        "promotion": measurement_action.get("promotion"),
                    }
                ),
                lease.trial_id,
            ),
        )
        cur.execute(
            """INSERT INTO charm_control.artifacts
            (artifact_id,trial_id,kind,relative_path,sha256,byte_size)
            VALUES (%s,%s,'durable-trial-json',%s,%s,%s)
            ON CONFLICT (trial_id,relative_path) DO UPDATE
            SET sha256=EXCLUDED.sha256,byte_size=EXCLUDED.byte_size""",
            (uuid.uuid4(), lease.trial_id, str(relative), digest, artifact.stat().st_size),
        )
        conn.commit()
    return {
        "artifact_relative_path": str(relative),
        "artifact_sha256": digest,
        "byte_size": artifact.stat().st_size,
        "feasible": bool(constraints["feasible"]),
    }


def _persist_health_result(
    settings: Settings, lease: TrialLease, health: dict[str, Any] | None
) -> dict[str, Any]:
    if health is None:
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT result FROM charm_control.trial_action_executions
                WHERE trial_id=%s AND state='VERIFYING_DATABASE_HEALTH' AND status='COMPLETED'""",
                (lease.trial_id,),
            )
            row = cur.fetchone()
            if row is None or row["result"] is None:
                raise RuntimeError("completed health action has no result")
            health = dict(row["result"])
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.trials
            SET workflow_result=%s,diagnostic_details=diagnostic_details || %s
            WHERE trial_id=%s""",
            (
                Jsonb(health),
                Jsonb({"health_checked_at": datetime.now(UTC).isoformat()}),
                lease.trial_id,
            ),
        )
        conn.commit()
    return health


def _complete_trial(settings: Settings, lease: TrialLease) -> None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.trials
            SET completed_at=clock_timestamp(),lease_owner=NULL,lease_token=NULL,
                lease_expires_at=NULL,heartbeat_at=NULL
            WHERE trial_id=%s AND lease_owner=%s AND lease_token=%s""",
            (lease.trial_id, lease.owner, lease.token),
        )
        if cur.rowcount != 1:
            raise RuntimeError("cannot complete trial without its lease")
        conn.commit()


def _fail_trial(
    settings: Settings,
    lease: TrialLease,
    error: Exception,
    terminal_state: str,
) -> None:
    if terminal_state not in TERMINAL_STATES:
        raise ValueError(f"invalid failure terminal state {terminal_state}")
    terminal = lease.attempt_count >= lease.max_attempts
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT state FROM charm_control.trials
            WHERE trial_id=%s AND lease_owner=%s AND lease_token=%s FOR UPDATE""",
            (lease.trial_id, lease.owner, lease.token),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("cannot fail trial without its lease") from error
        current = str(row["state"])
        if terminal:
            cur.execute(
                """INSERT INTO charm_control.trial_transitions
                (trial_id,from_state,to_state,reason,details)
                VALUES (%s,%s,%s,'retry limit reached',%s)""",
                (lease.trial_id, current, terminal_state, Jsonb({"error": str(error)})),
            )
            cur.execute(
                """UPDATE charm_control.trials
                SET state=%s,failure_type=%s,
                    diagnostic_details=diagnostic_details || %s,completed_at=clock_timestamp(),
                    lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL
                WHERE trial_id=%s""",
                (
                    terminal_state,
                    terminal_state,
                    Jsonb({"error": str(error)}),
                    lease.trial_id,
                ),
            )
            cur.execute(
                """UPDATE charm_control.campaigns
                SET failure_count=failure_count+1,
                    status=CASE WHEN failure_count+1 >= failure_limit THEN 'FAILED' ELSE status END,
                    updated_at=clock_timestamp()
                WHERE campaign_id=%s""",
                (lease.campaign_id,),
            )
        else:
            delay = min(60, 2 ** (lease.attempt_count - 1))
            cur.execute(
                """UPDATE charm_control.trials
                SET diagnostic_details=diagnostic_details || %s,
                    next_attempt_at=clock_timestamp() + (%s * interval '1 second'),
                    lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL
                WHERE trial_id=%s""",
                (
                    Jsonb({"last_error": str(error), "retry_delay_seconds": delay}),
                    delay,
                    lease.trial_id,
                ),
            )
        conn.commit()












def _run_benchmark_once(
    settings: Settings,
    lease: TrialLease,
    stale: bool,
    lease_seconds: int,
    stop_after_state: str | None,
) -> WorkerResult:
    state = lease.state

    if (
        lease.workflow_kind in TUNED_BENCHMARK_WORKFLOWS
        and lease.attempt_count > 1
        and state in TUNED_PRE_MEASUREMENT_RETRY_STATES
    ):
        _run_action(
            settings,
            lease,
            f"RECOVERING_TUNED_CONFIGURATION_ATTEMPT_{lease.attempt_count}",
            lambda: _apply_tuned_configuration(settings, lease),
        )

    def stopped() -> WorkerResult | None:
        if stop_after_state == state:
            return WorkerResult(True, lease.trial_id, state, stale)
        return None

    if result := stopped():
        return result
    if state == "CREATED":
        next_state = (
            "RESTORING_CANDIDATE_DATASET"
            if lease.workflow_kind in V2_BENCHMARK_WORKFLOWS
            else "CAPTURING_WORKLOAD_CONTEXT"
        )
        _advance(
            settings,
            lease,
            state,
            next_state,
            "candidate lifecycle transition persisted before target inspection",
        )
        state = next_state
        if result := stopped():
            return result
    if state == "RESTORING_CANDIDATE_DATASET":
        preflight_id = uuid.UUID(str(lease.payload["preflight_id"]))
        restored = _run_action(
            settings,
            lease,
            state,
            lambda: candidate_restore_result_dict(
                ensure_candidate_dataset_restored(
                    settings,
                    lease.trial_id,
                    preflight_id,
                    experiment_arm_id=(
                        uuid.UUID(str(lease.payload["experiment_arm_id"]))
                        if lease.payload.get("experiment_arm_id")
                        else None
                    ),
                    budget_position=(
                        int(lease.payload["budget_position"])
                        if lease.payload.get("budget_position") is not None
                        else None
                    ),
                    restore_mechanism=str(
                        lease.payload.get("restore_mechanism", "logical-restore")
                    ),
                    physical_archive_id=(
                        uuid.UUID(str(lease.payload["physical_archive_id"]))
                        if lease.payload.get("physical_archive_id")
                        else None
                    ),
                )
            ),
        )
        _advance(
            settings,
            lease,
            state,
            "VERIFYING_CANDIDATE_BASELINE",
            "candidate dataset restored and fingerprinted",
            {"restore_id": str(restored["restore_id"])},
        )
        state = "VERIFYING_CANDIDATE_BASELINE"
        if result := stopped():
            return result
    if state == "VERIFYING_CANDIDATE_BASELINE":
        verified_restore = _run_action(
            settings,
            lease,
            state,
            lambda: verify_trial_candidate_restore(settings, lease.trial_id),
        )
        _advance(
            settings,
            lease,
            state,
            "CAPTURING_WORKLOAD_CONTEXT",
            "passed candidate restore link verified",
            {"restore_id": verified_restore["restore_id"]},
        )
        state = "CAPTURING_WORKLOAD_CONTEXT"
        if result := stopped():
            return result
    if state == "CAPTURING_WORKLOAD_CONTEXT":
        _run_action(
            settings,
            lease,
            state,
            lambda: _capture_benchmark_context(settings, lease),
        )
        _advance(settings, lease, state, "VALIDATING_ACTIONS", "context captured")
        state = "VALIDATING_ACTIONS"
        if result := stopped():
            return result
    if state == "VALIDATING_ACTIONS":
        _run_action(
            settings,
            lease,
            state,
            lambda: _validate_benchmark_actions(settings, lease),
        )
        _advance(settings, lease, state, "ESTIMATING_STATIC_RISK", "baseline actions valid")
        state = "ESTIMATING_STATIC_RISK"
        if result := stopped():
            return result
    if state == "ESTIMATING_STATIC_RISK":
        _run_action(
            settings,
            lease,
            state,
            lambda: {
                "statically_feasible": True,
                "mutation_count": len(lease.payload.get("requested_configuration", {})),
                "restart_required": bool(lease.payload.get("requires_restart", False)),
            },
        )
        next_state = (
            "APPLYING_KNOBS"
            if lease.workflow_kind in TUNED_BENCHMARK_WORKFLOWS
            else "VERIFYING_DATABASE_HEALTH"
        )
        _advance(
            settings,
            lease,
            state,
            next_state,
            "benchmark action passed static risk",
        )
        state = next_state
        if result := stopped():
            return result
    if state == "APPLYING_KNOBS":
        _run_action(
            settings,
            lease,
            state,
            lambda: _apply_tuned_configuration(settings, lease),
        )
        _advance(
            settings,
            lease,
            state,
            "RELOADING_OR_RESTARTING",
            "idempotent configuration application verified",
        )
        state = "RELOADING_OR_RESTARTING"
        if result := stopped():
            return result
    if state == "RELOADING_OR_RESTARTING":
        _run_action(
            settings,
            lease,
            state,
            lambda: _verify_tuned_activation(settings, lease),
        )
        _advance(
            settings,
            lease,
            state,
            "VERIFYING_DATABASE_HEALTH",
            "reload or restart activation record verified",
        )
        state = "VERIFYING_DATABASE_HEALTH"
        if result := stopped():
            return result
    if state == "VERIFYING_DATABASE_HEALTH":
        _run_action(settings, lease, state, lambda: _health_snapshot(settings))
        _advance(
            settings,
            lease,
            state,
            "VERIFYING_ACTIVE_CONFIGURATION",
            "target health verified",
        )
        state = "VERIFYING_ACTIVE_CONFIGURATION"
        if result := stopped():
            return result
    if state == "VERIFYING_ACTIVE_CONFIGURATION":
        _run_action(
            settings,
            lease,
            state,
            lambda: _verify_baseline_configuration(settings, lease),
        )
        _advance(settings, lease, state, "WARMING_UP", "expected configuration verified active")
        state = "WARMING_UP"
        if result := stopped():
            return result
    if state == "WARMING_UP":

        def warm_up() -> dict[str, Any]:
            duration = int(lease.payload["warmup_seconds"])
            heartbeat(settings, lease, lease_seconds)
            poll_interval = max(1.0, min(5.0, lease_seconds / 3))
            application_name = _workload_application_name(lease, "warmup")
            orphan_cleanup = _terminate_orphan_workload_sessions(settings, application_name)

            def renew_lease() -> None:
                heartbeat(settings, lease, lease_seconds)

            run_pgbench_warmup(
                settings,
                duration,
                int(lease.payload["concurrency"]),
                int(lease.payload["seed"]),
                renew_lease,
                poll_interval,
                application_name,
                client_threads=(
                    int(lease.payload["client_threads"])
                    if lease.payload.get("client_threads") is not None
                    else None
                ),
            )
            return {
                "warmup_seconds": duration,
                "completed": True,
                "pgbench_maintenance_policy": PGBENCH_MAINTENANCE_POLICY,
                "pgbench_maintenance_arguments": ["--no-vacuum"],
                "orphan_cleanup": orphan_cleanup,
            }

        _run_action(settings, lease, state, warm_up)
        _advance(
            settings,
            lease,
            state,
            "RESETTING_OR_SNAPSHOTTING_COUNTERS",
            "warm-up completed",
        )
        state = "RESETTING_OR_SNAPSHOTTING_COUNTERS"
        if result := stopped():
            return result
    if state == "RESETTING_OR_SNAPSHOTTING_COUNTERS":
        _run_action(
            settings,
            lease,
            state,
            lambda: _persist_metric_snapshot(settings, lease, "before"),
        )
        _advance(
            settings,
            lease,
            state,
            "RUNNING_FULL_EVALUATION",
            "pre-measurement counters persisted",
        )
        state = "RUNNING_FULL_EVALUATION"
        if result := stopped():
            return result
    if state == "RUNNING_FULL_EVALUATION":
        _run_action(
            settings,
            lease,
            state,
            lambda: _execute_or_recover_measurement(settings, lease, lease_seconds),
        )
        _advance(settings, lease, state, "COLLECTING_METRICS", "client workload completed")
        state = "COLLECTING_METRICS"
        if result := stopped():
            return result
    if state == "COLLECTING_METRICS":
        _run_action(
            settings,
            lease,
            state,
            lambda: _persist_metric_snapshot(settings, lease, "after"),
        )
        _advance(
            settings,
            lease,
            state,
            "VALIDATING_MEASUREMENT",
            "post-measurement counters persisted",
        )
        state = "VALIDATING_MEASUREMENT"
        if result := stopped():
            return result
    if state == "VALIDATING_MEASUREMENT":
        _run_action(
            settings,
            lease,
            state,
            lambda: _validate_measurement(settings, lease),
        )
        _advance(
            settings,
            lease,
            state,
            "CALCULATING_OBJECTIVES",
            "measurement validated",
        )
        state = "CALCULATING_OBJECTIVES"
        if result := stopped():
            return result
    if state == "CALCULATING_OBJECTIVES":
        _run_action(
            settings,
            lease,
            state,
            lambda: _calculate_benchmark_objectives(settings, lease),
        )
        _advance(
            settings,
            lease,
            state,
            "CALCULATING_CONSTRAINTS",
            "objectives calculated",
        )
        state = "CALCULATING_CONSTRAINTS"
        if result := stopped():
            return result
    if state == "CALCULATING_CONSTRAINTS":
        _run_action(
            settings,
            lease,
            state,
            lambda: _calculate_benchmark_constraints(settings, lease),
        )
        _advance(
            settings,
            lease,
            state,
            "PERSISTING_OBSERVATION",
            "constraints calculated",
        )
        state = "PERSISTING_OBSERVATION"
        if result := stopped():
            return result
    if state == "PERSISTING_OBSERVATION":
        persisted = _run_action(
            settings,
            lease,
            state,
            lambda: _persist_benchmark_observation(settings, lease),
        )
        if lease.workflow_kind in TUNED_BENCHMARK_WORKFLOWS:
            target = "SELECTING_NEXT_ACTION"
        elif lease.workflow_kind == SATURATION_PHASE1_WORKFLOW:
            target = "COMPLETED" if persisted["feasible"] else "MEASUREMENT_INVALID"
        else:
            target = "COMPLETED" if persisted["feasible"] else "SLO_VIOLATED"
        _advance(settings, lease, state, target, "durable benchmark observation persisted")
        state = target
        if result := stopped():
            return result
    if state == "SELECTING_NEXT_ACTION":
        persisted = _benchmark_action_result(settings, lease, "PERSISTING_OBSERVATION")
        _run_action(
            settings,
            lease,
            state,
            lambda: _rollback_tuned_configuration(settings, lease),
        )
        target = "COMPLETED" if persisted["feasible"] else "SLO_VIOLATED"
        if target == "SLO_VIOLATED":
            target = "ROLLED_BACK"
        _advance(settings, lease, state, target, "last-known-good configuration restored")
        state = target
    if state in {"COMPLETED", "SLO_VIOLATED", "ROLLED_BACK"}:
        _complete_trial(settings, lease)
    return WorkerResult(True, lease.trial_id, state, stale)


def run_once(
    settings: Settings,
    owner: str | None = None,
    lease_seconds: int = 30,
    stop_after_state: str | None = None,
    campaign_id: uuid.UUID | None = None,
) -> WorkerResult:
    worker_id = owner or default_worker_id()
    lease, stale = claim_next_trial(settings, worker_id, lease_seconds, campaign_id)
    if lease is None:
        return WorkerResult(False, None, None, False)
    if lease.workflow_kind in {
        "BASELINE_BENCHMARK",
        "TUNED_BENCHMARK",
        "V2_BASELINE_BENCHMARK",
        "V2_TUNED_BENCHMARK",
        SATURATION_PHASE1_WORKFLOW,
    }:
        try:
            return _run_benchmark_once(settings, lease, stale, lease_seconds, stop_after_state)
        except Exception as error:
            state = lease.state
            with connect(settings.control_dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT state FROM charm_control.trials WHERE trial_id=%s", (lease.trial_id,)
                )
                row = cur.fetchone()
                if row is not None:
                    state = str(row["state"])
            if isinstance(error, ResourceLimitError):
                terminal = "RESOURCE_LIMIT_EXCEEDED"
            elif state == "RESTORING_CANDIDATE_DATASET":
                terminal = "DATASET_RESTORE_FAILED"
            elif state == "VERIFYING_CANDIDATE_BASELINE":
                terminal = "BASELINE_FINGERPRINT_FAILED"
            elif state == "APPLYING_KNOBS":
                terminal = "APPLY_FAILED"
            elif state == "RELOADING_OR_RESTARTING":
                terminal = "RESTART_FAILED"
            elif state in {
                "CAPTURING_WORKLOAD_CONTEXT",
                "VALIDATING_ACTIONS",
                "ESTIMATING_STATIC_RISK",
                "VERIFYING_ACTIVE_CONFIGURATION",
            }:
                terminal = "INVALID_CONFIGURATION"
            elif state == "VERIFYING_DATABASE_HEALTH":
                terminal = "DATABASE_UNHEALTHY"
            elif state in {"WARMING_UP", "RUNNING_FULL_EVALUATION"}:
                terminal = "WORKLOAD_FAILED"
            else:
                terminal = "MEASUREMENT_INVALID"
            if lease.workflow_kind in TUNED_BENCHMARK_WORKFLOWS:
                application_id, _snapshot_id = _application_ids(lease.trial_id)
                try:
                    with suppress(ValueError):
                        rollback_configuration(
                            settings,
                            application_id,
                            f"rollback after durable worker error: {error}",
                        )
                except Exception as rollback_error:
                    error = RuntimeError(f"{error}; automatic rollback failed: {rollback_error}")
            _fail_trial(settings, lease, error, terminal)
            raise
    if lease.workflow_kind != "HEALTH_CHECK":
        _fail_trial(
            settings,
            lease,
            ValueError(f"unsupported workflow {lease.workflow_kind}"),
            "INVALID_CONFIGURATION",
        )
        return WorkerResult(True, lease.trial_id, lease.state, stale)
    state = lease.state
    health: dict[str, Any] | None = None
    try:
        if state == "CREATED":
            _advance(
                settings,
                lease,
                "CREATED",
                "VERIFYING_DATABASE_HEALTH",
                "health action persisted before target query",
            )
            state = "VERIFYING_DATABASE_HEALTH"
            if stop_after_state == state:
                return WorkerResult(True, lease.trial_id, state, stale)
        if state == "VERIFYING_DATABASE_HEALTH":
            already_complete = _record_action_start(settings, lease, state)
            if not already_complete:
                health = _health_snapshot(settings)
                _record_action_complete(settings, lease, state, health)
            _advance(
                settings,
                lease,
                state,
                "PERSISTING_OBSERVATION",
                "target health verified",
            )
            state = "PERSISTING_OBSERVATION"
            if stop_after_state == state:
                return WorkerResult(True, lease.trial_id, state, stale)
        if state == "PERSISTING_OBSERVATION":
            already_complete = _record_action_start(settings, lease, state)
            if not already_complete:
                result = _persist_health_result(settings, lease, health)
                _record_action_complete(settings, lease, state, result)
            _advance(settings, lease, state, "COMPLETED", "health observation persisted")
            state = "COMPLETED"
            _complete_trial(settings, lease)
        return WorkerResult(True, lease.trial_id, state, stale)
    except Exception as error:
        _fail_trial(settings, lease, error, "DATABASE_UNHEALTHY")
        raise


def trial_history(settings: Settings, trial_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT trial_id,campaign_id,state,workflow_kind,workflow_payload,workflow_result,
                      attempt_count,max_attempts,lease_owner,lease_expires_at,failure_type,
                      diagnostic_details,created_at,started_at,completed_at
               FROM charm_control.trials WHERE trial_id=%s""",
            (trial_id,),
        )
        trial = cur.fetchone()
        if trial is None:
            raise ValueError(f"unknown trial {trial_id}")
        cur.execute(
            """SELECT transition_id,from_state,to_state,reason,details,occurred_at
               FROM charm_control.trial_transitions WHERE trial_id=%s ORDER BY transition_id""",
            (trial_id,),
        )
        transitions = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT state,idempotency_key,status,attempt,result,error,started_at,completed_at
               FROM charm_control.trial_action_executions
               WHERE trial_id=%s ORDER BY started_at""",
            (trial_id,),
        )
        actions = [dict(row) for row in cur.fetchall()]
    payload = dict(trial)
    payload["trial_id"] = str(payload["trial_id"])
    payload["campaign_id"] = str(payload["campaign_id"])
    payload["transitions"] = transitions
    payload["actions"] = actions
    return payload


def result_json(result: WorkerResult) -> str:
    return json.dumps(
        {
            "claimed": result.claimed,
            "trial_id": str(result.trial_id) if result.trial_id else None,
            "state": result.state,
            "recovered_stale_lease": result.recovered_stale_lease,
        },
        indent=2,
    )


def run_until_idle(
    settings: Settings, owner: str | None = None, poll_seconds: float = 0.25
) -> list[WorkerResult]:
    results: list[WorkerResult] = []
    while True:
        result = run_once(settings, owner=owner)
        if not result.claimed:
            return results
        results.append(result)
        time.sleep(max(0.0, poll_seconds))


def run_worker_service(
    settings: Settings,
    owner: str | None = None,
    lease_seconds: int = 30,
    poll_seconds: float = 1.0,
    campaign_id: uuid.UUID | None = None,
    shutdown_event: Event | None = None,
    idle_exit_seconds: float | None = None,
    max_trials: int | None = None,
    event_callback: Callable[[dict[str, Any]], None] | None = None,
) -> WorkerServiceResult:
    if lease_seconds < 5:
        raise ValueError("lease_seconds must be at least five")
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    if idle_exit_seconds is not None and idle_exit_seconds < 0:
        raise ValueError("idle_exit_seconds cannot be negative")
    if max_trials is not None and max_trials < 1:
        raise ValueError("max_trials must be positive")
    worker_id = owner or default_worker_id()
    stop = shutdown_event or Event()
    started = time.monotonic()
    idle_started: float | None = None
    processed = 0
    idle_polls = 0
    state_counts: dict[str, int] = {}

    def emit(event: str, **details: Any) -> None:
        if event_callback is not None:
            event_callback(
                {
                    "event": event,
                    "owner": worker_id,
                    "occurred_at": datetime.now(UTC).isoformat(),
                    **details,
                }
            )

    emit(
        "WORKER_SERVICE_STARTED",
        lease_seconds=lease_seconds,
        poll_seconds=poll_seconds,
        campaign_id=str(campaign_id) if campaign_id else None,
    )
    exit_reason = "shutdown_requested"
    while not stop.is_set():
        if max_trials is not None and processed >= max_trials:
            exit_reason = "max_trials_reached"
            break
        result = run_once(
            settings,
            owner=worker_id,
            lease_seconds=lease_seconds,
            campaign_id=campaign_id,
        )
        if result.claimed:
            processed += 1
            idle_started = None
            state = result.state or "UNKNOWN"
            state_counts[state] = state_counts.get(state, 0) + 1
            emit(
                "WORKER_TRIAL_FINISHED",
                trial_id=str(result.trial_id) if result.trial_id else None,
                state=result.state,
                recovered_stale_lease=result.recovered_stale_lease,
                processed_trials=processed,
            )
            continue
        idle_polls += 1
        now = time.monotonic()
        idle_started = idle_started or now
        if idle_exit_seconds is not None and now - idle_started >= idle_exit_seconds:
            exit_reason = "idle_timeout"
            break
        wait_seconds = poll_seconds
        if idle_exit_seconds is not None:
            wait_seconds = min(wait_seconds, max(0.0, idle_exit_seconds - (now - idle_started)))
        stop.wait(wait_seconds)
    if stop.is_set():
        exit_reason = "shutdown_requested"
    elapsed = time.monotonic() - started
    emit(
        "WORKER_SERVICE_STOPPED",
        processed_trials=processed,
        state_counts=state_counts,
        idle_polls=idle_polls,
        elapsed_seconds=elapsed,
        exit_reason=exit_reason,
    )
    return WorkerServiceResult(
        worker_id,
        processed,
        state_counts,
        idle_polls,
        elapsed,
        stop.is_set(),
        exit_reason,
    )


def service_result_json(result: WorkerServiceResult) -> str:
    return json.dumps(asdict(result), indent=2)
