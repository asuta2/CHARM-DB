from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.db import connect
from charmdb.worker import TERMINAL_STATES as WORKER_TERMINAL_STATES

TERMINAL_TRIAL_STATES = set(WORKER_TERMINAL_STATES)
MEASURED_F3_TERMINAL_STATES = ("COMPLETED", "SLO_VIOLATED", "ROLLED_BACK")
SECRET_TOKENS = ("password", "secret", "credential", "token", "dsn")
PLACEHOLDER_TOKENS = ("REPLACE_WITH", "TODO", "TBD")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class GateAssessment:
    gate: str
    eligible: bool
    reasons: tuple[str, ...]
    evidence: dict[str, Any]


@dataclass(frozen=True)
class ArmPreparation:
    arm_id: uuid.UUID
    campaign_id: uuid.UUID | None
    status: str
    manifest_sha256: str
    gate: GateAssessment


def canonical_manifest_sha256(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(body.encode()).hexdigest()


def _json_ready(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _secret_paths(value: Any, path: str = "execution") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if any(token in key.lower() for token in SECRET_TOKENS):
                paths.append(child_path)
            paths.extend(_secret_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_secret_paths(child, f"{path}[{index}]"))
    return paths


def validate_execution_definition(execution: dict[str, Any], budget_value: float) -> None:
    required = {
        "dataset",
        "workload_contexts",
        "execution_profile",
        "search_space",
        "method_parameters",
        "resource_limits",
        "cache_policy",
        "software_versions",
        "budget_accounting",
        "objective_definition",
        "constraint_definition",
        "practical_margins",
    }
    missing = sorted(required - execution.keys())
    if missing:
        raise ValueError(f"execution manifest is missing sections: {', '.join(missing)}")
    if not isinstance(execution["dataset"], dict) or not execution["dataset"]:
        raise ValueError("execution manifest requires a non-empty dataset definition")
    dataset = execution["dataset"]
    snapshot = dataset.get("snapshot")
    schema_sha256 = dataset.get("schema_sha256")
    scale = dataset.get("scale")
    if not isinstance(snapshot, str) or not snapshot.strip():
        raise ValueError("dataset.snapshot must identify an immutable snapshot")
    if not isinstance(schema_sha256, str) or not SHA256_PATTERN.fullmatch(schema_sha256):
        raise ValueError("dataset.schema_sha256 must be a lowercase SHA-256 digest")
    if not isinstance(scale, int) or scale < 1:
        raise ValueError("dataset.scale must be a positive integer")
    contexts = execution["workload_contexts"]
    if (
        not isinstance(contexts, list)
        or not contexts
        or not all(isinstance(item, dict) for item in contexts)
    ):
        raise ValueError("execution manifest requires one or more workload-context definitions")
    profile = execution["execution_profile"]
    if not isinstance(profile, dict):
        raise ValueError("execution_profile must be an object")
    warmup = profile.get("warmup_seconds")
    measurement = profile.get("measurement_seconds")
    concurrency = profile.get("concurrency")
    if not isinstance(warmup, (int, float)) or warmup < 0:
        raise ValueError("execution_profile.warmup_seconds must be non-negative")
    if not isinstance(measurement, (int, float)) or measurement <= 0:
        raise ValueError("execution_profile.measurement_seconds must be positive")
    if not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("execution_profile.concurrency must be a positive integer")
    search_space = execution["search_space"]
    if not isinstance(search_space, dict) or search_space.get("version") != "reload-knobs-v1":
        raise ValueError("search_space.version must be reload-knobs-v1")
    expected_parameters = {
        "random_page_cost": {"type": "continuous", "lower": 1.0, "upper": 4.0},
        "work_mem": {"type": "categorical", "values": [1024, 2048, 4096, 8192, 16384, 32768]},
        "effective_io_concurrency": {"type": "integer", "lower": 0, "upper": 200},
    }
    if search_space.get("parameters") != expected_parameters:
        raise ValueError(
            "search_space parameters do not match the executable reload-knobs-v1 space"
        )
    method_parameters = execution["method_parameters"]
    if not isinstance(method_parameters, dict):
        raise ValueError("method_parameters must be an object")
    if method_parameters.get("initial_observations") != 4:
        raise ValueError("method_parameters.initial_observations must be four")
    for name, minimum in (("candidate_pool_size", 16), ("posterior_samples", 16)):
        value = method_parameters.get(name)
        if not isinstance(value, int) or value < minimum:
            raise ValueError(f"method_parameters.{name} must be an integer >= {minimum}")
    max_attempts = method_parameters.get("max_attempts")
    if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 20:
        raise ValueError("method_parameters.max_attempts must be between one and twenty")
    limits = execution["resource_limits"]
    if not isinstance(limits, dict):
        raise ValueError("resource_limits must be an object")
    for name in ("target_cpus", "target_memory_bytes", "docker_memory_bytes"):
        value = limits.get(name)
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"resource_limits.{name} must be positive")
    cache_policy = execution["cache_policy"]
    if not isinstance(cache_policy, str) or not cache_policy.strip():
        raise ValueError("cache_policy must be a non-empty preregistered policy")
    versions = execution["software_versions"]
    if not isinstance(versions, dict) or not versions:
        raise ValueError("software_versions must be a non-empty object")
    accounting = execution["budget_accounting"]
    if not isinstance(accounting, dict):
        raise ValueError("budget_accounting must be an object")
    kind = accounting.get("kind")
    tolerance = accounting.get("relative_tolerance")
    if not isinstance(tolerance, (int, float)) or not 0 <= tolerance <= 0.05:
        raise ValueError("budget relative_tolerance must be between 0 and 0.05")
    if kind == "F3_EQUIVALENT_WALL_CLOCK":
        reference = accounting.get("f3_reference_wall_clock_seconds")
        if not isinstance(reference, (int, float)) or reference <= 0:
            raise ValueError("F3-equivalent accounting requires a positive F3 reference")
    elif kind == "DRIFT_PHASE_UNITS":
        targets = accounting.get("phase_targets")
        if not isinstance(targets, dict) or set(targets) != {"pre_drift", "recovery"}:
            raise ValueError("drift accounting requires pre_drift and recovery phase targets")
        if not all(isinstance(value, (int, float)) and value > 0 for value in targets.values()):
            raise ValueError("drift phase targets must be positive")
        if abs(sum(float(value) for value in targets.values()) - budget_value) > 1e-9:
            raise ValueError("drift phase targets must sum to the registered arm budget")
    else:
        raise ValueError(f"unsupported budget accounting kind {kind!r}")
    objective = execution["objective_definition"]
    if not isinstance(objective, dict):
        raise ValueError("objective_definition must be an object")
    reference = objective.get("reference_point")
    if (
        not isinstance(reference, list)
        or len(reference) != 2
        or not all(isinstance(value, (int, float)) for value in reference)
    ):
        raise ValueError("objective_definition.reference_point must contain two numeric values")
    margins = execution["practical_margins"]
    if not isinstance(margins, dict) or set(margins) != {
        "throughput_relative",
        "p99_ms_absolute",
        "hypervolume_relative",
    }:
        raise ValueError("practical_margins must freeze throughput, p99, and hypervolume margins")
    if not all(isinstance(value, (int, float)) and value >= 0 for value in margins.values()):
        raise ValueError("practical margins must be non-negative numbers")
    secret_paths = _secret_paths(execution)
    if secret_paths:
        raise ValueError(f"execution manifest contains secret-like keys: {', '.join(secret_paths)}")
    try:
        serialized = json.dumps(execution, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("execution manifest must be JSON serializable") from exc
    if any(token in serialized.upper() for token in PLACEHOLDER_TOKENS):
        raise ValueError("execution manifest contains unresolved placeholder values")


def decide_prerequisite_gate(gate: str, evidence: dict[str, Any]) -> GateAssessment:
    reasons: list[str] = []
    if gate == "measurement_validity":
        if int(evidence.get("measured_f3_trials", 0)) < 5:
            reasons.append("fewer than five completed F3 measurements")
        if int(evidence.get("active_configuration_matched_f3_trials", 0)) < 5:
            reasons.append("fewer than five F3 trials matched requested and active settings")
        if int(evidence.get("artifact_backed_f3_trials", 0)) < 5:
            reasons.append("fewer than five F3 measurements have hash-verified raw artifacts")
    elif gate == "paired_fidelity_gate":
        if int(evidence.get("paired_candidates", 0)) < 12:
            reasons.append("fewer than twelve paired-fidelity candidates")
        if not bool(evidence.get("early_stopping_enabled", False)):
            reasons.append("paired-fidelity early-stopping gate is closed")
    elif gate == "calibration_gate":
        if int(evidence.get("infeasible_labels", 0)) < 1:
            reasons.append("calibration evidence has no infeasible labels")
        if not bool(evidence.get("probabilistic_promotion_enabled", False)):
            reasons.append("probabilistic calibration gate is closed")
    elif gate == "coordination_execution_gate":
        if int(evidence.get("completed_joint_action_trials", 0)) < 1:
            reasons.append("no completed trial has both knob and managed-index actions")
    elif gate == "drift_execution_gate":
        if int(evidence.get("completed_transfer_reports", 0)) < 1:
            reasons.append("no completed fixed-budget transfer/adaptation report exists")
    elif gate == "cost_measurement_gate":
        if int(evidence.get("cost_observations", 0)) < 5:
            reasons.append("fewer than five complete operational-cost observations")
        variants = set(str(value) for value in evidence.get("cost_variants", []))
        required = {"no_cost", "benchmark_only", "restart_index_only", "complete"}
        if not required.issubset(variants):
            reasons.append("all four cost-decision variants have not been persisted")
    elif gate == "full_pipeline_gate":
        component_gates = evidence.get("component_gates", {})
        if not isinstance(component_gates, dict):
            reasons.append("full-pipeline component-gate evidence is malformed")
        else:
            closed = sorted(name for name, value in component_gates.items() if not bool(value))
            if closed:
                reasons.append(f"full-pipeline prerequisites are closed: {', '.join(closed)}")
    else:
        reasons.append(f"unknown prerequisite gate {gate}")
    return GateAssessment(gate, not reasons, tuple(reasons), evidence)


def evaluate_prerequisite_gate(settings: Settings, gate: str) -> GateAssessment:
    evidence: dict[str, Any]
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        if gate == "measurement_validity":
            cur.execute(
                """
                SELECT t.trial_id,t.requested_configuration,t.active_configuration,
                       a.relative_path,a.sha256,a.byte_size
                FROM charm_control.trials t
                LEFT JOIN charm_control.artifacts a USING(trial_id)
                WHERE t.fidelity=3 AND t.completed_at IS NOT NULL
                  AND t.state = ANY(%s)
                  AND COALESCE(t.objective_values->>'throughput_tps',
                               t.workflow_result->>'throughput_tps') IS NOT NULL
                  AND COALESCE(t.objective_values->>'p99_ms',
                               t.workflow_result->>'p99_ms') IS NOT NULL
                """,
                (list(MEASURED_F3_TERMINAL_STATES),),
            )
            rows = [dict(row) for row in cur.fetchall()]
            measured_ids = {str(row["trial_id"]) for row in rows}
            matched_ids = {
                str(row["trial_id"])
                for row in rows
                if all(
                    str(row["active_configuration"].get(name)) == str(value)
                    for name, value in row["requested_configuration"].items()
                )
            }
            artifact_checks: dict[str, list[bool]] = {}
            mismatches = 0
            artifact_root = settings.artifact_dir.resolve()
            for row in rows:
                if row["relative_path"] is None:
                    continue
                path = (settings.artifact_dir / str(row["relative_path"])).resolve()
                try:
                    path.relative_to(artifact_root)
                except ValueError:
                    artifact_checks.setdefault(str(row["trial_id"]), []).append(False)
                    mismatches += 1
                    continue
                valid = (
                    path.is_file()
                    and path.stat().st_size == int(row["byte_size"])
                    and hashlib.sha256(path.read_bytes()).hexdigest() == str(row["sha256"])
                )
                artifact_checks.setdefault(str(row["trial_id"]), []).append(valid)
                if not valid:
                    mismatches += 1
            verified_ids = {
                trial_id for trial_id, checks in artifact_checks.items() if checks and all(checks)
            }
            evidence = {
                "measured_f3_trials": len(measured_ids),
                "active_configuration_matched_f3_trials": len(matched_ids),
                "artifact_backed_f3_trials": len(verified_ids),
                "artifact_hash_mismatches": mismatches,
            }
        elif gate == "paired_fidelity_gate":
            cur.execute(
                """SELECT report_id,paired_candidates,promotion_false_negative,
                          early_stopping_enabled,created_at
                   FROM charm_control.fidelity_reports ORDER BY created_at DESC LIMIT 1"""
            )
            evidence = dict(cur.fetchone() or {})
        elif gate == "calibration_gate":
            cur.execute(
                """SELECT report_id,scored_candidates,infeasible_labels,
                          probabilistic_promotion_enabled,created_at
                   FROM charm_control.calibration_reports ORDER BY created_at DESC LIMIT 1"""
            )
            evidence = dict(cur.fetchone() or {})
        elif gate == "coordination_execution_gate":
            cur.execute(
                """
                SELECT count(*) AS completed_joint_action_trials
                FROM charm_control.trials t
                JOIN charm_control.trial_actions a USING(trial_id)
                WHERE t.state='COMPLETED' AND a.knob_configuration <> '{}'::jsonb
                  AND cardinality(a.indexes_created)>0
                """
            )
            evidence = dict(cur.fetchone() or {})
        elif gate == "drift_execution_gate":
            cur.execute(
                "SELECT count(*) AS completed_transfer_reports FROM charm_control.transfer_reports"
            )
            evidence = dict(cur.fetchone() or {})
        elif gate == "cost_measurement_gate":
            cur.execute("SELECT count(*) AS cost_observations FROM charm_control.cost_observations")
            evidence = dict(cur.fetchone() or {})
            cur.execute(
                "SELECT DISTINCT variant FROM charm_control.cost_decisions ORDER BY variant"
            )
            evidence["cost_variants"] = [str(row["variant"]) for row in cur.fetchall()]
        elif gate == "full_pipeline_gate":
            components = (
                "measurement_validity",
                "paired_fidelity_gate",
                "calibration_gate",
                "coordination_execution_gate",
                "drift_execution_gate",
                "cost_measurement_gate",
            )
            component_results = {
                name: evaluate_prerequisite_gate(settings, name).eligible for name in components
            }
            evidence = {"component_gates": component_results}
        else:
            evidence = {}
    return decide_prerequisite_gate(gate, evidence)


def prerequisite_gate_status(settings: Settings) -> list[GateAssessment]:
    gates = (
        "measurement_validity",
        "paired_fidelity_gate",
        "calibration_gate",
        "coordination_execution_gate",
        "drift_execution_gate",
        "cost_measurement_gate",
        "full_pipeline_gate",
    )
    return [evaluate_prerequisite_gate(settings, gate) for gate in gates]


def _arm_manifest(arm: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "registry": {
            "arm_id": str(arm["arm_id"]),
            "group_id": str(arm["group_id"]),
            "group_key": arm["group_key"],
            "group_kind": arm["group_kind"],
            "hypothesis": arm["hypothesis"],
            "baseline": arm["baseline"],
            "label": arm["label"],
            "method_definition": arm["method_definition"],
            "random_seed": arm["random_seed"],
            "block_order": arm["block_order"],
            "budget_unit": arm["budget_unit"],
            "budget_value": arm["budget_value"],
            "required_metrics": arm["required_metrics"],
            "prerequisite_gate": arm["prerequisite_gate"],
        },
        "execution": execution,
    }


def prepare_experiment_arm(
    settings: Settings,
    arm_id: uuid.UUID,
    execution: dict[str, Any],
    actor: str = "cli",
) -> ArmPreparation:
    if not actor.strip():
        raise ValueError("actor cannot be empty")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.*,g.group_key,g.group_kind,g.hypothesis,g.baseline,g.budget_unit,
                   g.required_metrics,g.prerequisite_gate,g.status AS group_status
            FROM charm_control.experiment_arms a
            JOIN charm_control.experiment_groups g USING(group_id)
            WHERE a.arm_id=%s
            """,
            (arm_id,),
        )
        arm = cur.fetchone()
    if arm is None:
        raise ValueError(f"unknown experiment arm {arm_id}")
    validate_execution_definition(execution, float(arm["budget_value"]))
    manifest = _arm_manifest(dict(arm), execution)
    digest = canonical_manifest_sha256(manifest)
    lineage = execution.get("evidence_lineage")
    if lineage is not None:
        if not isinstance(lineage, dict) or not isinstance(lineage.get("preflight_id"), str):
            raise ValueError("execution evidence_lineage must contain a preflight_id")
        try:
            preflight_id = uuid.UUID(lineage["preflight_id"])
        except ValueError as exc:
            raise ValueError("execution evidence_lineage.preflight_id must be a UUID") from exc
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT review_id FROM charm_control.experiment_manifest_reviews
                   WHERE preflight_id=%s AND arm_id=%s AND frozen_manifest_sha256=%s AND passed
                   ORDER BY created_at DESC LIMIT 1""",
                (preflight_id, arm_id, digest),
            )
            review = cur.fetchone()
        if review is None:
            raise ValueError(
                "evidence-derived execution manifest has no matching passing independent review"
            )
    gate = evaluate_prerequisite_gate(settings, str(arm["prerequisite_gate"]))
    gate_payload = {
        "gate": gate.gate,
        "eligible": gate.eligible,
        "reasons": list(gate.reasons),
        "evidence": gate.evidence,
        "evaluated_at": datetime.now(UTC).isoformat(),
    }
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.*,g.group_key,g.status AS group_status
            FROM charm_control.experiment_arms a
            JOIN charm_control.experiment_groups g USING(group_id)
            WHERE a.arm_id=%s FOR UPDATE OF a,g
            """,
            (arm_id,),
        )
        locked = cur.fetchone()
        if locked is None:
            raise ValueError(f"unknown experiment arm {arm_id}")
        existing_digest = locked["manifest_sha256"]
        if existing_digest is not None and str(existing_digest) != digest:
            raise ValueError("experiment arm already has a different immutable manifest")
        if locked["status"] in {"RUNNING", "COMPLETED", "FAILED"}:
            raise ValueError(f"cannot prepare experiment arm in state {locked['status']}")
        if not gate.eligible:
            reason = "; ".join(gate.reasons)
            cur.execute(
                """UPDATE charm_control.experiment_arms
                   SET status='BLOCKED',gate_snapshot=%s,failure_reason=%s WHERE arm_id=%s""",
                (Jsonb(_json_ready(gate_payload)), reason, arm_id),
            )
            cur.execute(
                """UPDATE charm_control.experiment_groups
                   SET status='BLOCKED',blocked_reason=%s WHERE group_id=%s
                     AND status NOT IN ('RUNNING','COMPLETED','FAILED')""",
                (reason, locked["group_id"]),
            )
            if locked["status"] != "BLOCKED":
                cur.execute(
                    """INSERT INTO charm_control.experiment_arm_events
                       (arm_id,event_type,previous_status,new_status,actor,reason,details)
                       VALUES (%s,'GATE_BLOCKED',%s,'BLOCKED',%s,%s,%s)""",
                    (arm_id, locked["status"], actor, reason, Jsonb(_json_ready(gate_payload))),
                )
            conn.commit()
            return ArmPreparation(arm_id, None, "BLOCKED", digest, gate)
        if existing_digest is None:
            cur.execute(
                """UPDATE charm_control.experiment_arms
                   SET execution_manifest=%s,manifest_sha256=%s,gate_snapshot=%s
                   WHERE arm_id=%s""",
                (Jsonb(manifest), digest, Jsonb(_json_ready(gate_payload)), arm_id),
            )
            cur.execute(
                """INSERT INTO charm_control.experiment_arm_events
                   (arm_id,event_type,previous_status,new_status,actor,reason,details)
                   VALUES (%s,'MANIFEST_FROZEN',%s,%s,%s,
                           'immutable execution manifest frozen',%s)""",
                (arm_id, locked["status"], locked["status"], actor, Jsonb({"sha256": digest})),
            )
        campaign_id = locked["campaign_id"]
        if campaign_id is None:
            campaign_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:experiment-campaign:{arm_id}")
            execution_profile = execution["execution_profile"]
            cur.execute(
                """INSERT INTO charm_control.campaigns
                   (campaign_id,name,mode,status,objective_definition,constraint_definition,
                    settings,failure_limit)
                   VALUES (%s,%s,'EXPERIMENT','CREATED',%s,%s,%s,%s)""",
                (
                    campaign_id,
                    f"experiment-{locked['group_key']}-{arm['label']}-{arm['random_seed']}",
                    Jsonb(execution["objective_definition"]),
                    Jsonb(execution["constraint_definition"]),
                    Jsonb(
                        {
                            "experiment_arm_id": str(arm_id),
                            "manifest_sha256": digest,
                            "execution_profile": execution_profile,
                        }
                    ),
                    int(execution_profile.get("failure_limit", 5)),
                ),
            )
            cur.execute(
                """INSERT INTO charm_control.campaign_events
                   (campaign_id,event_type,previous_status,new_status,actor,reason,details)
                   VALUES (%s,'CREATE',NULL,'CREATED',%s,
                           'experiment campaign prepared from immutable arm manifest',%s)""",
                (campaign_id, actor, Jsonb({"arm_id": str(arm_id), "manifest_sha256": digest})),
            )
        previous = str(locked["status"])
        cur.execute(
            """UPDATE charm_control.experiment_arms
               SET campaign_id=%s,status='PREPARED',gate_snapshot=%s,failure_reason=NULL
               WHERE arm_id=%s""",
            (campaign_id, Jsonb(_json_ready(gate_payload)), arm_id),
        )
        cur.execute(
            """UPDATE charm_control.experiment_groups
               SET status='ELIGIBLE',blocked_reason=NULL WHERE group_id=%s
                 AND status NOT IN ('RUNNING','COMPLETED','FAILED')""",
            (locked["group_id"],),
        )
        if previous != "PREPARED":
            cur.execute(
                """INSERT INTO charm_control.experiment_arm_events
                   (arm_id,event_type,previous_status,new_status,actor,reason,details)
                   VALUES (%s,'PREPARED',%s,'PREPARED',%s,
                           'campaign linked after prerequisite gate passed',%s)""",
                (
                    arm_id,
                    previous,
                    actor,
                    Jsonb(_json_ready({"campaign_id": str(campaign_id), "gate": gate_payload})),
                ),
            )
        conn.commit()
    return ArmPreparation(arm_id, uuid.UUID(str(campaign_id)), "PREPARED", digest, gate)


def _budget_breakdown(rows: list[dict[str, Any]]) -> dict[str, Any]:
    phases: dict[str, float] = {}
    fidelities: dict[str, float] = {}
    wall_clock_seconds = 0.0
    for row in rows:
        unit = float(row["unit_value"])
        phase = str(row["phase"])
        fidelity = str(row["fidelity"])
        phases[phase] = phases.get(phase, 0.0) + unit
        fidelities[fidelity] = fidelities.get(fidelity, 0.0) + unit
        wall_clock_seconds += float(row["wall_clock_seconds"])
    return {
        "entries": len(rows),
        "phases": phases,
        "fidelities": fidelities,
        "wall_clock_seconds": wall_clock_seconds,
    }


def f3_equivalent_unit_value(
    wall_clock_seconds: float,
    reference_seconds: float,
    fidelity: int,
    objective_values: dict[str, Any],
) -> float:
    if wall_clock_seconds < 0 or reference_seconds <= 0:
        raise ValueError(
            "F3-equivalent accounting requires non-negative cost and positive reference"
        )
    measured_units = wall_clock_seconds / reference_seconds
    full_f3_measurement = (
        fidelity == 3
        and objective_values.get("throughput_tps") is not None
        and objective_values.get("p99_ms") is not None
    )
    return max(1.0, measured_units) if full_f3_measurement else measured_units


def record_experiment_trial_budget(
    settings: Settings,
    arm_id: uuid.UUID,
    trial_id: uuid.UUID,
    phase: str = "static",
    actor: str = "cli",
) -> dict[str, Any]:
    if not phase.strip():
        raise ValueError("budget phase cannot be empty")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT arm_id,campaign_id,status,execution_manifest
               FROM charm_control.experiment_arms WHERE arm_id=%s FOR UPDATE""",
            (arm_id,),
        )
        arm = cur.fetchone()
        if arm is None:
            raise ValueError(f"unknown experiment arm {arm_id}")
        if arm["campaign_id"] is None or arm["execution_manifest"] is None:
            raise ValueError("experiment arm must be prepared before budget can be recorded")
        if arm["status"] in {"COMPLETED", "FAILED"}:
            raise ValueError(f"cannot record budget for experiment arm in state {arm['status']}")
        cur.execute(
            """SELECT trial_id,campaign_id,state,fidelity,created_at,started_at,completed_at,
                      failure_type,workflow_kind,objective_values
               FROM charm_control.trials WHERE trial_id=%s""",
            (trial_id,),
        )
        trial = cur.fetchone()
        if trial is None or trial["campaign_id"] != arm["campaign_id"]:
            raise ValueError("trial does not belong to the experiment arm campaign")
        if trial["completed_at"] is None or str(trial["state"]) not in TERMINAL_TRIAL_STATES:
            raise ValueError("only terminal trials with a completion timestamp consume budget")
        started = trial["started_at"] or trial["created_at"]
        wall_clock_seconds = max(0.0, (trial["completed_at"] - started).total_seconds())
        manifest = dict(arm["execution_manifest"])
        execution = dict(manifest["execution"])
        accounting = dict(execution["budget_accounting"])
        accounting_kind = str(accounting["kind"])
        if accounting_kind == "F3_EQUIVALENT_WALL_CLOCK":
            reference = float(accounting["f3_reference_wall_clock_seconds"])
            objectives = dict(trial["objective_values"] or {})
            unit_value = f3_equivalent_unit_value(
                wall_clock_seconds, reference, int(trial["fidelity"]), objectives
            )
        elif accounting_kind == "DRIFT_PHASE_UNITS":
            if phase not in {"pre_drift", "recovery"}:
                raise ValueError("drift budget entries must use pre_drift or recovery phase")
            unit_value = 1.0
        else:
            raise ValueError(f"unsupported budget accounting kind {accounting_kind}")
        cur.execute(
            """SELECT benchmark_seconds,restart_seconds,readiness_seconds,index_build_seconds,
                      index_drop_seconds,index_build_wal_bytes,index_storage_bytes,
                      configuration_churn,raw_details
               FROM charm_control.cost_observations WHERE trial_id=%s""",
            (trial_id,),
        )
        cost = cur.fetchone()
        operational_cost = dict(cost) if cost else {}
        cur.execute(
            """SELECT artifact_id,relative_path,sha256,byte_size
               FROM charm_control.artifacts WHERE trial_id=%s ORDER BY artifact_id""",
            (trial_id,),
        )
        artifacts = [dict(row) for row in cur.fetchall()]
        evidence = {
            "trial_state": trial["state"],
            "failure_type": trial["failure_type"],
            "workflow_kind": trial["workflow_kind"],
            "started_at": started,
            "completed_at": trial["completed_at"],
            "artifacts": artifacts,
            "budget_formula": (
                "max(1, measured_wall_clock/reference) for a valid full F3 measurement; "
                "measured_wall_clock/reference otherwise"
                if accounting_kind == "F3_EQUIVALENT_WALL_CLOCK"
                else "one registered drift phase unit"
            ),
        }
        budget_entry_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:budget:{arm_id}:{trial_id}")
        cur.execute(
            """INSERT INTO charm_control.experiment_budget_entries
               (budget_entry_id,arm_id,trial_id,phase,fidelity,wall_clock_seconds,unit_value,
                accounting_kind,operational_cost,evidence)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (arm_id,trial_id) DO NOTHING""",
            (
                budget_entry_id,
                arm_id,
                trial_id,
                phase,
                trial["fidelity"],
                wall_clock_seconds,
                unit_value,
                accounting_kind,
                Jsonb(_json_ready(operational_cost)),
                Jsonb(_json_ready(evidence)),
            ),
        )
        inserted = bool(cur.rowcount)
        if not inserted:
            cur.execute(
                """SELECT phase,fidelity,wall_clock_seconds,unit_value,accounting_kind
                   FROM charm_control.experiment_budget_entries
                   WHERE arm_id=%s AND trial_id=%s""",
                (arm_id, trial_id),
            )
            existing = cur.fetchone()
            if existing is None or (
                str(existing["phase"]) != phase
                or int(existing["fidelity"]) != int(trial["fidelity"])
                or str(existing["accounting_kind"]) != accounting_kind
                or abs(float(existing["wall_clock_seconds"]) - wall_clock_seconds) > 1e-9
                or abs(float(existing["unit_value"]) - unit_value) > 1e-9
            ):
                raise ValueError("existing budget entry conflicts with terminal trial evidence")
        cur.execute(
            """SELECT phase,fidelity,wall_clock_seconds,unit_value
               FROM charm_control.experiment_budget_entries
               WHERE arm_id=%s ORDER BY created_at,budget_entry_id""",
            (arm_id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
        realized = sum(float(row["unit_value"]) for row in rows)
        breakdown = _budget_breakdown(rows)
        cur.execute(
            """UPDATE charm_control.experiment_arms
               SET realized_budget_value=%s,budget_breakdown=%s,
                   status=CASE WHEN status='PREPARED' THEN 'RUNNING' ELSE status END,
                   started_at=COALESCE(started_at,clock_timestamp())
               WHERE arm_id=%s""",
            (realized, Jsonb(breakdown), arm_id),
        )
        if inserted:
            cur.execute(
                """INSERT INTO charm_control.experiment_arm_events
                   (arm_id,event_type,previous_status,new_status,actor,reason,details)
                   VALUES (%s,'BUDGET_RECORDED',%s,'RUNNING',%s,
                           'terminal trial charged to realized experiment budget',%s)""",
                (
                    arm_id,
                    arm["status"],
                    actor,
                    Jsonb(
                        {
                            "budget_entry_id": str(budget_entry_id),
                            "trial_id": str(trial_id),
                            "phase": phase,
                            "unit_value": unit_value,
                            "trial_state": trial["state"],
                        }
                    ),
                ),
            )
        conn.commit()
    return {
        "budget_entry_id": str(budget_entry_id),
        "arm_id": str(arm_id),
        "trial_id": str(trial_id),
        "inserted": inserted,
        "realized_budget_value": realized,
        "budget_breakdown": breakdown,
    }


def finalize_experiment_arm(
    settings: Settings, arm_id: uuid.UUID, actor: str = "cli"
) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT a.*,g.group_id,g.status AS group_status
               FROM charm_control.experiment_arms a
               JOIN charm_control.experiment_groups g USING(group_id)
               WHERE a.arm_id=%s FOR UPDATE OF a,g""",
            (arm_id,),
        )
        arm = cur.fetchone()
        if arm is None:
            raise ValueError(f"unknown experiment arm {arm_id}")
        if arm["status"] == "COMPLETED":
            return {
                "arm_id": str(arm_id),
                "campaign_id": str(arm["campaign_id"]),
                "status": "COMPLETED",
                "realized_budget_value": float(arm["realized_budget_value"]),
            }
        if arm["campaign_id"] is None or arm["execution_manifest"] is None:
            raise ValueError("experiment arm has no prepared campaign and manifest")
        cur.execute(
            """SELECT status FROM charm_control.campaigns WHERE campaign_id=%s FOR UPDATE""",
            (arm["campaign_id"],),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise ValueError("linked experiment campaign is missing")
        cur.execute(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE completed_at IS NULL OR NOT (state=ANY(%s))) AS active
               FROM charm_control.trials WHERE campaign_id=%s""",
            (list(TERMINAL_TRIAL_STATES), arm["campaign_id"]),
        )
        trial_counts = cur.fetchone()
        if trial_counts is None or int(trial_counts["total"]) < 1:
            raise ValueError("experiment campaign has no trials")
        if int(trial_counts["active"]) > 0:
            raise ValueError("experiment campaign still has non-terminal trials")
        manifest = dict(arm["execution_manifest"])
        execution = dict(manifest["execution"])
        accounting = dict(execution["budget_accounting"])
        target = float(arm["budget_value"])
        realized = float(arm["realized_budget_value"])
        tolerance = float(accounting["relative_tolerance"])
        if realized + 1e-9 < target:
            raise ValueError(f"realized budget {realized:.6f} is below target {target:.6f}")
        if realized - 1e-9 > target * (1 + tolerance):
            maximum = target * (1 + tolerance)
            raise ValueError(
                f"realized budget {realized:.6f} exceeds target/tolerance {maximum:.6f}"
            )
        if accounting["kind"] == "DRIFT_PHASE_UNITS":
            phases = dict(arm["budget_breakdown"].get("phases", {}))
            for phase, phase_target in dict(accounting["phase_targets"]).items():
                if abs(float(phases.get(phase, 0.0)) - float(phase_target)) > 1e-9:
                    raise ValueError(f"drift phase {phase} does not match its frozen target")
        if str(campaign["status"]) in {"STOPPED", "FAILED"}:
            raise ValueError(f"cannot complete arm from campaign state {campaign['status']}")
        previous = str(arm["status"])
        if str(campaign["status"]) != "COMPLETED":
            cur.execute(
                """UPDATE charm_control.campaigns
                   SET status='COMPLETED',updated_at=clock_timestamp() WHERE campaign_id=%s""",
                (arm["campaign_id"],),
            )
            cur.execute(
                """INSERT INTO charm_control.campaign_events
                   (campaign_id,event_type,previous_status,new_status,actor,reason,details)
                   VALUES (%s,'COMPLETE',%s,'COMPLETED',%s,%s,%s)""",
                (
                    arm["campaign_id"],
                    campaign["status"],
                    actor,
                    "experiment arm met frozen budget and terminal-trial requirements",
                    Jsonb({"arm_id": str(arm_id), "realized_budget_value": realized}),
                ),
            )
        cur.execute(
            """UPDATE charm_control.experiment_arms
               SET status='COMPLETED',completed_at=clock_timestamp(),failure_reason=NULL
               WHERE arm_id=%s""",
            (arm_id,),
        )
        cur.execute(
            """INSERT INTO charm_control.experiment_arm_events
               (arm_id,event_type,previous_status,new_status,actor,reason,details)
               VALUES (%s,'COMPLETED',%s,'COMPLETED',%s,
                       'frozen budget and terminal-trial requirements satisfied',%s)""",
            (arm_id, previous, actor, Jsonb({"realized_budget_value": realized})),
        )
        cur.execute(
            """SELECT count(*) AS incomplete FROM charm_control.experiment_arms
               WHERE group_id=%s AND status<>'COMPLETED'""",
            (arm["group_id"],),
        )
        remaining = cur.fetchone()
        if remaining is not None and int(remaining["incomplete"]) == 0:
            cur.execute(
                """UPDATE charm_control.experiment_groups
                   SET status='COMPLETED',completed_at=clock_timestamp(),blocked_reason=NULL
                   WHERE group_id=%s""",
                (arm["group_id"],),
            )
        else:
            cur.execute(
                """UPDATE charm_control.experiment_groups SET status='RUNNING'
                   WHERE group_id=%s AND status NOT IN ('COMPLETED','FAILED')""",
                (arm["group_id"],),
            )
        conn.commit()
    return {
        "arm_id": str(arm_id),
        "campaign_id": str(arm["campaign_id"]),
        "status": "COMPLETED",
        "realized_budget_value": realized,
    }


def experiment_arm_history(settings: Settings, arm_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT a.*,g.group_key,g.group_kind,g.budget_unit,g.prerequisite_gate
               FROM charm_control.experiment_arms a
               JOIN charm_control.experiment_groups g USING(group_id)
               WHERE a.arm_id=%s""",
            (arm_id,),
        )
        arm = cur.fetchone()
        if arm is None:
            raise ValueError(f"unknown experiment arm {arm_id}")
        cur.execute(
            """SELECT event_id,event_type,previous_status,new_status,actor,reason,details,
                      occurred_at
               FROM charm_control.experiment_arm_events WHERE arm_id=%s ORDER BY event_id""",
            (arm_id,),
        )
        events = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT budget_entry_id,trial_id,phase,fidelity,wall_clock_seconds,unit_value,
                      accounting_kind,operational_cost,evidence,created_at
               FROM charm_control.experiment_budget_entries
               WHERE arm_id=%s ORDER BY created_at,budget_entry_id""",
            (arm_id,),
        )
        budget_entries = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT recommendation_id,budget_position,method,stage,random_seed,input_vector,
                      configuration,training_observation_ids,acquisition_name,acquisition_value,
                      probability_feasible,trial_id,created_at
               FROM charm_control.experiment_search_recommendations
               WHERE arm_id=%s ORDER BY budget_position""",
            (arm_id,),
        )
        search_recommendations = [dict(row) for row in cur.fetchall()]
    return {
        "arm": dict(arm),
        "events": events,
        "budget_entries": budget_entries,
        "search_recommendations": search_recommendations,
    }
