from __future__ import annotations

import json
import math
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from psycopg.types.json import Jsonb

from charmdb.apply_best import (
    _active_configuration,
    _schema_and_deployment,
    apply_best_manifest_payload,
    champion_configuration,
)
from charmdb.campaigns.default_reference import (
    FROZEN_BASELINE_ID,
    FROZEN_CLIENT_THREADS,
    FROZEN_CONCURRENCY,
    FROZEN_PREFLIGHT_ID,
    FROZEN_RESTORE_MECHANISM,
)
from charmdb.campaigns.f4 import (
    INFRASTRUCTURE_FAILURES,
    TERMINAL_RUN_STATUSES,
    F4Slot,
    _artifact_path,
    _canonical_sha256,
    _csv_bytes,
    _f4_block,
    _file_sha256,
    _proposal_sha256,
    _summary,
    _target_fixture_gate,
    build_f4_plan,
)
from charmdb.config import Settings
from charmdb.controller import discover_knobs, validate_candidate
from charmdb.db import connect
from charmdb.protocol import load_manifest
from charmdb.restore.fingerprint import _load_preflight
from charmdb.restore.preflight import _target_safety
from charmdb.worker import (
    control_campaign,
    create_campaign,
    create_tuned_benchmark_trial,
    run_once,
)

H25_MANIFEST = Path("experiments/thesis/manifests/h25-heuristic-baseline.json")
H25_STAGE = "h25-heuristic-baseline"
H25_EVIDENCE_ROLE = "SECONDARY"
H25_PROFILE_ID = "scale500-c32-w600-h25-600-v1"
H25_DESIGN_KIND = "three-arm-heuristic-default-champion"
H25_TREATMENTS = ("H25", "DEFAULT", "E")
H25_CHAMPION = "E"
H25_SHARED_BUFFERS = "131072"
H25_BLOCKS = 4
H25_PER_BLOCK = 3
H25_OBSERVATIONS = 12
H25_CENTER_POSITION = 2
H25_ORDERS = [
    ["H25", "DEFAULT", "E"],
    ["E", "DEFAULT", "H25"],
    ["E", "DEFAULT", "H25"],
    ["H25", "DEFAULT", "E"],
]
EVALUATION_ROLES = {"DEFAULT": "H25_DEFAULT", "H25": "H25_TREATMENT", "E": "H25_CHAMPION"}
OWNER = "v2-h25-heuristic-baseline"
IN_FLIGHT_DEPLOYMENT_STATUSES = frozenset({"RECOVERY_TESTING", "ACTIVATING", "ROLLING_BACK"})
LIVE_BLOCK_STATUSES = frozenset(
    {"PLANNED", "RUNNING", "PAUSED_INFRASTRUCTURE", "OBSERVATIONS_COMPLETE"}
)
SUMMARY_METRICS = (
    ("raw_default_throughput_tps", "default_throughput_tps"),
    ("raw_default_p99_ms", "default_p99_ms"),
    ("raw_h25_throughput_tps", "h25_throughput_tps"),
    ("raw_h25_p99_ms", "h25_p99_ms"),
    ("raw_champion_throughput_tps", "champion_throughput_tps"),
    ("raw_champion_p99_ms", "champion_p99_ms"),
    ("h25_control_relative_tps_percent", "h25_relative_tps_percent"),
    ("h25_control_relative_p99_ms", "h25_p99_delta_ms"),
    ("champion_control_relative_tps_percent", "champion_relative_tps_percent"),
    ("champion_control_relative_p99_ms", "champion_p99_delta_ms"),
    ("h25_vs_champion_tps_percent", "h25_vs_champion_tps_percent"),
    ("h25_vs_champion_p99_ms", "h25_vs_champion_p99_delta_ms"),
    ("share_of_champion_tps_gain", "share_of_champion_tps_gain"),
    ("share_of_champion_p99_gain", "share_of_champion_p99_gain"),
)


@dataclass(frozen=True)
class H25Step:
    action: str
    campaign_id: uuid.UUID
    h25_block_id: uuid.UUID
    h25_run_id: uuid.UUID | None
    trial_id: uuid.UUID | None
    details: dict[str, Any]


def _configuration(values: object) -> dict[str, str]:
    return {str(name): str(value) for name, value in dict(cast(dict[str, Any], values)).items()}


def h25_manifest_payload(manifest_path: Path = H25_MANIFEST) -> tuple[str, dict[str, Any]]:
    manifest = load_manifest(manifest_path)
    if manifest.stage != H25_STAGE or manifest.evidence_role != H25_EVIDENCE_ROLE:
        raise ValueError("H25 manifest has the wrong stage or evidence role")
    payload = manifest.payload
    treatments = payload["treatments"]
    if set(treatments) != set(H25_TREATMENTS):
        raise ValueError("H25 manifest must define exactly the DEFAULT, H25, and E treatments")
    default = _configuration(treatments["DEFAULT"]["requested_configuration"])
    heuristic = _configuration(treatments["H25"]["requested_configuration"])
    if treatments["H25"].get("changed_knob") != "shared_buffers" or heuristic != {
        **default,
        "shared_buffers": H25_SHARED_BUFFERS,
    }:
        raise ValueError("H25 treatment must change only shared_buffers to 131072 pages")
    champion = treatments[H25_CHAMPION]
    if champion.get("f4_label") != H25_CHAMPION or champion.get("source_primary_run_id") != payload[
        "source_evidence"
    ].get("champion_source_primary_run_id"):
        raise ValueError("H25 champion arm must identify F4 champion E and its source run")
    profile = payload["benchmark_profile"]
    if (
        profile.get("profile_id") != H25_PROFILE_ID
        or profile.get("warmup_seconds") != 600
        or profile.get("measurement_seconds") != 600
        or profile.get("concurrency") != FROZEN_CONCURRENCY
        or profile.get("client_threads") != FROZEN_CLIENT_THREADS
        or profile.get("restore_mechanism") != FROZEN_RESTORE_MECHANISM
    ):
        raise ValueError("H25 benchmark profile differs from the F4-derived frozen profile")
    design = payload["design"]
    if (
        design.get("design_kind") != H25_DESIGN_KIND
        or design.get("physical_observations") != H25_OBSERVATIONS
        or design.get("observations_per_block") != H25_PER_BLOCK
        or design.get("default_at_center_position") is not True
    ):
        raise ValueError("H25 design differs from the three-arm default-at-center layout")
    if design["infrastructure_retry_policy"].get("maximum_trials_per_slot") != 3:
        raise ValueError("H25 retry policy differs from F4")
    if [list(block["order"]) for block in design["schedule"]] != H25_ORDERS:
        raise ValueError("H25 schedule must be the counterbalanced outer-position design")
    return _file_sha256(manifest.path), payload


def build_h25_plan(manifest_path: Path = H25_MANIFEST) -> tuple[F4Slot, ...]:
    _, payload = h25_manifest_payload(manifest_path)
    f4_plan = build_f4_plan()
    defaults = next(slot.configuration for slot in f4_plan if slot.treatment == "DEFAULT")
    f4_champion = next(slot for slot in f4_plan if slot.treatment == H25_CHAMPION)
    f4_seeds = {slot.repetition_block: slot.random_seed for slot in f4_plan}
    treatments = payload["treatments"]
    configurations = {
        str(label): _configuration(row["requested_configuration"])
        for label, row in treatments.items()
    }
    if configurations["DEFAULT"] != defaults:
        raise ValueError("H25 DEFAULT differs from the frozen F4 PostgreSQL default")
    champion_source = uuid.UUID(str(treatments[H25_CHAMPION]["source_primary_run_id"]))
    if (
        configurations[H25_CHAMPION] != f4_champion.configuration
        or champion_source != f4_champion.source_primary_run_id
    ):
        raise ValueError("H25 champion arm differs from the frozen F4 finalist E")
    sources: dict[str, uuid.UUID | None] = {label: None for label in H25_TREATMENTS}
    sources[H25_CHAMPION] = champion_source
    slots: list[F4Slot] = []
    position = 0
    for block in payload["design"]["schedule"]:
        block_number = int(block["block"])
        seed = int(block["seed"])
        if seed != f4_seeds.get(block_number):
            raise ValueError(f"H25 block {block_number} does not reuse the F4 block seed")
        for within_position, treatment in enumerate(block["order"], start=1):
            position += 1
            label = str(treatment)
            slots.append(
                F4Slot(
                    position,
                    block_number,
                    within_position,
                    label,
                    sources[label],
                    seed,
                    configurations[label],
                )
            )
    if len(slots) != H25_OBSERVATIONS:
        raise ValueError("H25 design must contain exactly 12 physical observations")
    for block_number in range(1, H25_BLOCKS + 1):
        rows = [slot for slot in slots if slot.repetition_block == block_number]
        if len(rows) != H25_PER_BLOCK or {slot.treatment for slot in rows} != set(H25_TREATMENTS):
            raise ValueError(f"H25 block {block_number} is not complete")
        if len({slot.random_seed for slot in rows}) != 1:
            raise ValueError(f"H25 block {block_number} does not use one common workload seed")
        center = next(slot for slot in rows if slot.within_block_position == H25_CENTER_POSITION)
        if center.treatment != "DEFAULT":
            raise ValueError(f"H25 block {block_number} must hold DEFAULT at the center position")
    for label in ("H25", H25_CHAMPION):
        positions = sorted(slot.within_block_position for slot in slots if slot.treatment == label)
        if positions != [1, 1, 3, 3]:
            raise ValueError(f"H25 treatment {label} is not counterbalanced across outer positions")
    return tuple(slots)


def _f4_lineage_gate(
    settings: Settings, payload: dict[str, Any], plan: tuple[F4Slot, ...]
) -> dict[str, Any]:
    source = payload["source_evidence"]
    try:
        block: dict[str, Any] | None = _f4_block(settings, uuid.UUID(str(source["f4_campaign_id"])))
    except ValueError:
        block = None
    analysis = dict((block or {}).get("analysis") or {})
    f4_seeds = {
        int(row["repetition_block"]): int(row["random_seed"])
        for row in analysis.get("observations", [])
        if row.get("treatment") == "DEFAULT"
    }
    plan_seeds = {slot.repetition_block: slot.random_seed for slot in plan}
    seeds_match = f4_seeds == plan_seeds
    passed = bool(
        block
        and block["status"] == "ANALYZED"
        and block["campaign_status"] == "STOPPED"
        and str(block["f4_block_id"]) == source["f4_block_id"]
        and str(block["manifest_sha256"]) == source["f4_manifest_sha256"]
        and str(block["analysis_sha256"]) == source["f4_analysis_sha256"]
        and _canonical_sha256(analysis) == source["f4_analysis_sha256"]
        and analysis.get("decision") == source["f4_required_decision"]
        and analysis.get("selected_treatment") == source["f4_champion_treatment"]
        and str(analysis.get("selected_primary_run_id")) == source["champion_source_primary_run_id"]
        and seeds_match
    )
    return {
        "passed": passed,
        "block": (
            {
                key: block[key]
                for key in (
                    "f4_block_id",
                    "status",
                    "campaign_status",
                    "manifest_sha256",
                    "analysis_sha256",
                )
            }
            if block
            else None
        ),
        "decision": analysis.get("decision"),
        "selected_treatment": analysis.get("selected_treatment"),
        "selected_primary_run_id": analysis.get("selected_primary_run_id"),
        "seeds_match": seeds_match,
    }


def _apply_best_gate(settings: Settings) -> dict[str, Any]:
    installed, row = _schema_and_deployment(settings)
    if row is None:
        return {
            "passed": True,
            "installed": installed,
            "deployment": None,
            "champion_active": False,
            "in_flight": False,
        }
    _, payload = apply_best_manifest_payload()
    active = _active_configuration(settings, champion_configuration(payload))
    status = str(row["status"])
    champion_active = bool(active["matches_requested"])
    in_flight = status in IN_FLIGHT_DEPLOYMENT_STATUSES
    return {
        "passed": not champion_active and not in_flight and not active["pending_restart"],
        "installed": installed,
        "deployment": {"deployment_id": str(row["deployment_id"]), "status": status},
        "champion_active": champion_active,
        "in_flight": in_flight,
        "active_configuration": active,
    }


def _differing_settings(expected: dict[str, str], live: dict[str, str]) -> list[str]:
    return sorted(name for name, value in expected.items() if live.get(name) != value)


def _canonical_settings_gate(settings: Settings) -> dict[str, Any]:
    preflight = _load_preflight(settings, FROZEN_PREFLIGHT_ID)
    expected = {
        str(name): str(dict(definition).get("setting"))
        for name, definition in dict(preflight["evidence"].get("active_defaults") or {}).items()
    }
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name,setting FROM pg_settings WHERE name = ANY(%s) ORDER BY name",
            (list(expected),),
        )
        live = {str(row["name"]): str(row["setting"]) for row in cur.fetchall()}
    differing = _differing_settings(expected, live)
    return {
        "passed": not differing,
        "preflight_id": str(FROZEN_PREFLIGHT_ID),
        "expected": expected,
        "live": live,
        "differing": differing,
    }


def _require_canonical_target(settings: Settings) -> None:
    gate = _canonical_settings_gate(settings)
    if not gate["passed"]:
        raise ValueError(
            "H25 target is not at the canonical preflight settings "
            f"(differing: {gate['differing']}); roll back the active champion deployment "
            "before running another slot"
        )


def _schema_state(settings: Settings) -> tuple[bool, bool]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('charm_control.experiment_v2_h25_blocks') IS NOT NULL AS installed"
        )
        tables = bool(cur.fetchone()["installed"])  # type: ignore[index]
        cur.execute(
            """SELECT count(*) AS count FROM information_schema.columns
               WHERE table_schema='charm_control' AND table_name='experiment_v2_h25_runs'
                 AND column_name='source_primary_run_id'"""
        )
        three_arm = int(cur.fetchone()["count"]) == 1  # type: ignore[index]
    return tables, three_arm


def _blocks_by_liveness(
    settings: Settings,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT b.h25_block_id,b.campaign_id,b.status,c.status AS campaign_status
               FROM charm_control.experiment_v2_h25_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.status<>'FAILED' ORDER BY b.created_at DESC LIMIT 1"""
        )
        live = cur.fetchone()
        cur.execute(
            """SELECT b.h25_block_id,b.campaign_id,b.status,c.status AS campaign_status
               FROM charm_control.experiment_v2_h25_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.status='FAILED' ORDER BY b.completed_at DESC LIMIT 1"""
        )
        failed = cur.fetchone()
    return (dict(live) if live else None, dict(failed) if failed else None)


def h25_readiness(settings: Settings, manifest_path: Path = H25_MANIFEST) -> dict[str, Any]:
    manifest_sha256, payload = h25_manifest_payload(manifest_path)
    tables_installed, three_arm_installed = _schema_state(settings)
    existing, superseded = _blocks_by_liveness(settings) if tables_installed else (None, None)
    plan = build_h25_plan(manifest_path)
    metadata = discover_knobs(settings, set(plan[0].configuration))
    for slot in plan:
        validate_candidate(slot.configuration, metadata)
    target = _target_safety(settings, require_idle=False)
    artifact_path = settings.artifact_dir.resolve()
    usage = shutil.disk_usage(artifact_path)
    lineage_gate = _f4_lineage_gate(settings, payload, plan)
    apply_best_gate = _apply_best_gate(settings)
    canonical_gate = _canonical_settings_gate(settings)
    fixture_gate = _target_fixture_gate(settings)
    blockers: list[str] = []
    if payload["status"] != "ready" or payload.get("execution_ready") is not True:
        blockers.append("manifest-not-execution-ready")
    prerequisites = payload["prerequisites"]
    if prerequisites.get("durable_h25_runner_implemented") is not True:
        blockers.append("durable-runner-not-frozen")
    if prerequisites.get("result_blind_h25_analysis_implemented") is not True:
        blockers.append("result-blind-analysis-not-frozen")
    if not tables_installed:
        blockers.append("migration-045-not-applied")
    elif not three_arm_installed:
        blockers.append("migration-046-not-applied")
    if not lineage_gate["passed"]:
        blockers.append("f4-lineage-not-authenticated")
    if apply_best_gate["champion_active"]:
        blockers.append("apply-best-champion-active-on-target")
    elif not apply_best_gate["passed"]:
        blockers.append("apply-best-deployment-in-flight")
    if not canonical_gate["passed"]:
        blockers.append("target-settings-differ-from-canonical-preflight")
    if not fixture_gate["passed"]:
        blockers.append("target-has-index-integration-test-fixture")
    if existing is not None:
        blockers.append("h25-block-already-exists")
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
        "design_kind": H25_DESIGN_KIND,
        "schema_installed": tables_installed,
        "three_arm_schema_installed": three_arm_installed,
        "f4_lineage_gate": lineage_gate,
        "apply_best_gate": apply_best_gate,
        "canonical_settings_gate": canonical_gate,
        "test_fixture_gate": fixture_gate,
        "slots": len(plan),
        "existing_block": existing,
        "superseded_block": superseded,
        "target_safety": target,
        "artifact_directory": str(artifact_path),
        "artifact_free_bytes": usage.free,
    }


def create_h25_plan(settings: Settings, manifest_path: Path = H25_MANIFEST) -> uuid.UUID:
    readiness = h25_readiness(settings, manifest_path)
    if readiness["ready"] is not True:
        raise ValueError(f"H25 is blocked by readiness gates: {readiness['blockers']}")
    _, payload = h25_manifest_payload(manifest_path)
    source = payload["source_evidence"]
    superseded = readiness["superseded_block"]
    superseded_id = uuid.UUID(str(superseded["h25_block_id"])) if superseded is not None else None
    campaign_id = create_campaign(
        settings,
        OWNER,
        "H25_HEURISTIC_BASELINE",
        {"maximize": "throughput_tps", "minimize": "p99_ms"},
        {"benchmark_failures_must_equal": 0, "p99_slo_applied": False},
        failure_limit=H25_OBSERVATIONS,
        campaign_settings={
            "protocol_id": "thesis-protocol-v2",
            "stage": H25_STAGE,
            "design_kind": H25_DESIGN_KIND,
            "manifest_sha256": readiness["manifest_sha256"],
            "manifest": payload,
            "f4_campaign_id": source["f4_campaign_id"],
            "f4_analysis_sha256": source["f4_analysis_sha256"],
            "supersedes_h25_block_id": str(superseded_id) if superseded_id else None,
        },
        actor=OWNER,
    )
    block_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{campaign_id}:{H25_STAGE}")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_h25_blocks
               (h25_block_id,campaign_id,protocol_id,evidence_role,manifest_sha256,
                f4_campaign_id,f4_block_id,f4_manifest_sha256,f4_analysis_sha256,
                preflight_id,baseline_id,benchmark_profile_id,design_kind,
                supersedes_h25_block_id,status)
               VALUES (%s,%s,'thesis-protocol-v2',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PLANNED')""",
            (
                block_id,
                campaign_id,
                H25_EVIDENCE_ROLE,
                readiness["manifest_sha256"],
                uuid.UUID(str(source["f4_campaign_id"])),
                uuid.UUID(str(source["f4_block_id"])),
                source["f4_manifest_sha256"],
                source["f4_analysis_sha256"],
                FROZEN_PREFLIGHT_ID,
                FROZEN_BASELINE_ID,
                H25_PROFILE_ID,
                H25_DESIGN_KIND,
                superseded_id,
            ),
        )
        for slot in build_h25_plan(manifest_path):
            run_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"charmdb:{block_id}:run:{slot.physical_position}"
            )
            cur.execute(
                """INSERT INTO charm_control.experiment_v2_h25_runs
                   (h25_run_id,h25_block_id,campaign_id,physical_position,repetition_block,
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


def _h25_block(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT b.*,c.status AS campaign_status
               FROM charm_control.experiment_v2_h25_blocks b
               JOIN charm_control.campaigns c USING(campaign_id) WHERE b.campaign_id=%s""",
            (campaign_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown H25 campaign {campaign_id}")
    return dict(row)


def _reconcile_attempt(settings: Settings, block_id: uuid.UUID) -> dict[str, Any] | None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.h25_run_id,a.h25_attempt_id,a.attempt_number,a.trial_id,
                      t.state,t.completed_at,t.failure_type,t.objective_values,
                      t.constraint_values,t.diagnostic_details
               FROM charm_control.experiment_v2_h25_runs r
               JOIN charm_control.experiment_v2_h25_attempts a
                 ON a.h25_run_id=r.h25_run_id AND a.status='CREATED'
               JOIN charm_control.trials t USING(trial_id)
               WHERE r.h25_block_id=%s AND r.status='CREATED'
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
            """UPDATE charm_control.experiment_v2_h25_attempts
               SET status=%s,failure_type=%s,failure_details=%s,completed_at=clock_timestamp()
               WHERE h25_attempt_id=%s AND status='CREATED'""",
            (attempt_status, failure_type, Jsonb(details), row["h25_attempt_id"]),
        )
        cur.execute(
            """UPDATE charm_control.experiment_v2_h25_runs
               SET status=%s,infrastructure_attempts=%s,failure_details=%s,
                   completed_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END
               WHERE h25_run_id=%s AND status='CREATED'""",
            (
                run_status,
                int(row["attempt_number"]),
                Jsonb(details if run_status != "COMPLETED" else {}),
                terminal,
                row["h25_run_id"],
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
) -> H25Step:
    run_id = uuid.UUID(str(row["h25_run_id"]))
    attempt_number = int(row["infrastructure_attempts"]) + 1
    trial_id = create_tuned_benchmark_trial(
        settings,
        campaign_id,
        FROZEN_PREFLIGHT_ID,
        _configuration(row["requested_configuration"]),
        int(row["random_seed"]),
        f"v2-h25:{run_id}:attempt:{attempt_number}",
        evidence_role=H25_EVIDENCE_ROLE,
        evaluation_role=EVALUATION_ROLES[str(row["treatment"])],
        warmup_seconds=600,
        duration_seconds=600,
        concurrency=FROZEN_CONCURRENCY,
        client_threads=FROZEN_CLIENT_THREADS,
        max_attempts=1,
        restore_mechanism=FROZEN_RESTORE_MECHANISM,
        benchmark_profile=H25_PROFILE_ID,
        runtime_samples_required=True,
    )
    attempt_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{run_id}:h25-attempt:{attempt_number}")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_h25_attempts
               (h25_attempt_id,h25_run_id,attempt_number,trial_id,status)
               VALUES (%s,%s,%s,%s,'CREATED')
               ON CONFLICT (h25_run_id,attempt_number) DO NOTHING""",
            (attempt_id, run_id, attempt_number, trial_id),
        )
        cur.execute(
            """UPDATE charm_control.experiment_v2_h25_runs SET status='CREATED'
               WHERE h25_run_id=%s AND status IN ('PROPOSED','RETRY_PENDING')""",
            (run_id,),
        )
        if cur.rowcount != 1:
            raise RuntimeError("H25 slot lost its executable state")
        conn.commit()
    result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
    finalized = _reconcile_attempt(settings, block_id)
    return H25Step(
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


def run_h25_next(
    settings: Settings,
    campaign_id: uuid.UUID,
    *,
    owner: str = OWNER,
    lease_seconds: int = 600,
) -> H25Step:
    block = _h25_block(settings, campaign_id)
    block_id = uuid.UUID(str(block["h25_block_id"]))
    reconciled = _reconcile_attempt(settings, block_id)
    if reconciled is not None and reconciled.get("run_status") == "INFRASTRUCTURE_EXHAUSTED":
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_h25_blocks SET status='PAUSED_INFRASTRUCTURE'
                   WHERE h25_block_id=%s AND status='RUNNING'""",
                (block_id,),
            )
            conn.commit()
        control_campaign(settings, campaign_id, "pause", "H25 retries exhausted", actor=owner)
        return H25Step(
            "infrastructure-paused",
            campaign_id,
            block_id,
            uuid.UUID(str(reconciled["h25_run_id"])),
            uuid.UUID(str(reconciled["trial_id"])),
            {"human_decision_required": True, "repetition_consumed": False},
        )
    if block["campaign_status"] != "RUNNING":
        raise ValueError(f"H25 campaign must be RUNNING, not {block['campaign_status']}")
    if block["status"] == "PLANNED":
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE charm_control.experiment_v2_h25_blocks SET status='RUNNING' "
                "WHERE h25_block_id=%s AND status='PLANNED'",
                (block_id,),
            )
            conn.commit()
    elif block["status"] != "RUNNING":
        raise ValueError(f"H25 block cannot run from {block['status']}")
    if reconciled is not None and reconciled.get("completed_at") is None:
        result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
        _reconcile_attempt(settings, block_id)
        return H25Step("slot-resumed", campaign_id, block_id, None, result.trial_id, {})
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_v2_h25_runs
               WHERE h25_block_id=%s AND status IN ('PROPOSED','RETRY_PENDING')
               ORDER BY physical_position LIMIT 1""",
            (block_id,),
        )
        row = cur.fetchone()
    if row is not None:
        _require_canonical_target(settings)
        return _execute_slot(settings, campaign_id, block_id, dict(row), owner, lease_seconds)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status,count(*) AS count FROM charm_control.experiment_v2_h25_runs "
            "WHERE h25_block_id=%s GROUP BY status",
            (block_id,),
        )
        counts = {str(item["status"]): int(item["count"]) for item in cur.fetchall()}
        if sum(counts.get(status, 0) for status in TERMINAL_RUN_STATUSES) != H25_OBSERVATIONS:
            raise RuntimeError(f"H25 has no runnable slot but is incomplete: {counts}")
        if counts.get("INFRASTRUCTURE_EXHAUSTED", 0):
            raise RuntimeError("H25 contains an infrastructure-exhausted slot")
        cur.execute(
            """UPDATE charm_control.experiment_v2_h25_blocks SET status='OBSERVATIONS_COMPLETE'
               WHERE h25_block_id=%s AND status='RUNNING'""",
            (block_id,),
        )
        conn.commit()
    control_campaign(
        settings, campaign_id, "pause", "all H25 observations are terminal", actor=owner
    )
    return H25Step("observations-complete", campaign_id, block_id, None, None, {"counts": counts})


def abandon_h25_block(settings: Settings, campaign_id: uuid.UUID, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("abandonment reason cannot be empty")
    block = _h25_block(settings, campaign_id)
    if block["status"] not in LIVE_BLOCK_STATUSES:
        raise ValueError(f"H25 block cannot be abandoned from {block['status']}")
    block_id = uuid.UUID(str(block["h25_block_id"]))
    pending = _reconcile_attempt(settings, block_id)
    if pending is not None and pending.get("completed_at") is None:
        raise ValueError("H25 block has a trial still in flight")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status,count(*) AS count FROM charm_control.experiment_v2_h25_runs "
            "WHERE h25_block_id=%s GROUP BY status",
            (block_id,),
        )
        counts = {str(item["status"]): int(item["count"]) for item in cur.fetchall()}
        if counts.get("COMPLETED", 0):
            raise ValueError("H25 block with completed observations cannot be abandoned")
        cur.execute(
            """SELECT count(*) AS count FROM charm_control.experiment_v2_h25_attempts a
               JOIN charm_control.experiment_v2_h25_runs r USING(h25_run_id)
               WHERE r.h25_block_id=%s AND a.status='INFRASTRUCTURE_FAILED'""",
            (block_id,),
        )
        retained = int(cur.fetchone()["count"])  # type: ignore[index]
        record = {
            "outcome": "ABANDONED",
            "reason": reason.strip(),
            "design_kind": block.get("design_kind"),
            "run_status_counts": counts,
            "retained_infrastructure_failures": retained,
        }
        record_sha256 = _canonical_sha256(record)
        cur.execute(
            """UPDATE charm_control.experiment_v2_h25_blocks
               SET status='FAILED',analysis=%s,analysis_sha256=%s,completed_at=clock_timestamp()
               WHERE h25_block_id=%s AND status=%s""",
            (Jsonb(record), record_sha256, block_id, block["status"]),
        )
        if cur.rowcount != 1:
            raise RuntimeError("H25 block lost its live state during abandonment")
        conn.commit()
    campaign = control_campaign(settings, campaign_id, "stop", reason.strip(), actor=OWNER)
    return {
        "h25_block_id": str(block_id),
        "campaign_id": str(campaign_id),
        "block_status": "FAILED",
        "campaign": campaign,
        "record": record,
        "record_sha256": record_sha256,
    }


def h25_history(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    block = _h25_block(settings, campaign_id)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.*,a.attempt_number,a.trial_id,a.status AS attempt_status,
                      t.state AS trial_state,t.objective_values,t.constraint_values
               FROM charm_control.experiment_v2_h25_runs r
               LEFT JOIN LATERAL (
                   SELECT * FROM charm_control.experiment_v2_h25_attempts a
                   WHERE a.h25_run_id=r.h25_run_id ORDER BY attempt_number DESC LIMIT 1
               ) a ON true LEFT JOIN charm_control.trials t USING(trial_id)
               WHERE r.h25_block_id=%s ORDER BY r.physical_position""",
            (block["h25_block_id"],),
        )
        runs = [dict(row) for row in cur.fetchall()]
    return {"block": block, "runs": runs}


def _load_authenticated_rows(
    settings: Settings, campaign_id: uuid.UUID
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    block = _h25_block(settings, campaign_id)
    if block["status"] != "OBSERVATIONS_COMPLETE" or block["campaign_status"] != "PAUSED":
        raise ValueError("H25 analysis requires a paused observations-complete block")
    if block.get("design_kind") != H25_DESIGN_KIND:
        raise ValueError("H25 analysis requires a three-arm block")
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
               FROM charm_control.experiment_v2_h25_runs r
               JOIN LATERAL (
                   SELECT * FROM charm_control.experiment_v2_h25_attempts a
                   WHERE a.h25_run_id=r.h25_run_id ORDER BY attempt_number DESC LIMIT 1
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
               WHERE r.h25_block_id=%s ORDER BY r.physical_position""",
            (block["h25_block_id"],),
        )
        rows = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT r.physical_position,a.attempt_number,a.status,a.failure_type,t.trial_id,
                      EXTRACT(EPOCH FROM (t.completed_at-t.created_at)) AS lifecycle_seconds
               FROM charm_control.experiment_v2_h25_attempts a
               JOIN charm_control.experiment_v2_h25_runs r USING(h25_run_id)
               JOIN charm_control.trials t USING(trial_id)
               WHERE r.h25_block_id=%s AND a.status='INFRASTRUCTURE_FAILED'
               ORDER BY r.physical_position,a.attempt_number""",
            (block["h25_block_id"],),
        )
        failed_attempts = [dict(row) for row in cur.fetchall()]
    if len(rows) != H25_OBSERVATIONS:
        raise ValueError("H25 analysis requires all 12 terminal slots")
    root = settings.artifact_dir.resolve()
    for row, expected in zip(rows, build_h25_plan(), strict=True):
        identity = (
            int(row["physical_position"]),
            int(row["repetition_block"]),
            int(row["within_block_position"]),
            str(row["treatment"]),
            uuid.UUID(str(row["source_primary_run_id"])) if row["source_primary_run_id"] else None,
            int(row["random_seed"]),
            _configuration(row["requested_configuration"]),
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
            raise ValueError(f"H25 frozen-plan mismatch at position {expected.physical_position}")
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
            raise ValueError(f"H25 hard-gate mismatch at position {expected.physical_position}")
        for name in ("throughput_tps", "p99_ms"):
            if not math.isfinite(float(objectives.get(name, math.nan))):
                raise ValueError(f"H25 invalid {name} at position {expected.physical_position}")
        artifact = _artifact_path(root, row["artifact_relative_path"])
        if (
            not artifact.is_file()
            or artifact.stat().st_size != int(row["artifact_byte_size"])
            or _file_sha256(artifact) != str(row["artifact_sha256"])
        ):
            raise ValueError(f"H25 artifact authentication failed: {artifact}")
        artifact_payload = json.loads(artifact.read_text(encoding="utf-8"))
        marker_relative = measurement.get("marker_relative_path")
        if (
            not marker_relative
            or workflow.get("measurement_marker") != marker_relative
            or artifact_payload.get("measurement_marker") != marker_relative
        ):
            raise ValueError("H25 measurement marker lineage mismatch")
        marker = _artifact_path(root, marker_relative)
        if not marker.is_file() or _file_sha256(marker) != str(measurement.get("marker_sha256")):
            raise ValueError(f"H25 marker authentication failed: {marker}")
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


def _objectives(row: dict[str, Any]) -> tuple[float, float]:
    objectives = dict(row["objective_values"])
    return float(objectives["throughput_tps"]), float(objectives["p99_ms"])


def _optional_float(row: dict[str, Any] | None, key: str) -> float | None:
    if row is None or row.get(key) is None:
        return None
    return float(row[key])


def _relative_percent(value: float | None, reference: float | None) -> float | None:
    if value is None or reference is None or reference == 0:
        return None
    return (value / reference - 1.0) * 100.0


def _share(value: float | None, reference: float | None) -> float | None:
    if value is None or reference is None or reference == 0:
        return None
    return value / reference


def _block_contrast(block: int, ordered: list[dict[str, Any]]) -> dict[str, Any]:
    by_treatment = {str(row["treatment"]): row for row in ordered}
    default_tps, default_p99 = _objectives(by_treatment["DEFAULT"])
    h25_tps, h25_p99 = _objectives(by_treatment["H25"])
    champion_tps, champion_p99 = _objectives(by_treatment[H25_CHAMPION])
    h25_gain = (h25_tps / default_tps - 1.0) * 100.0
    champion_gain = (champion_tps / default_tps - 1.0) * 100.0
    h25_p99_delta = h25_p99 - default_p99
    champion_p99_delta = champion_p99 - default_p99
    return {
        "repetition_block": block,
        "random_seed": int(by_treatment["DEFAULT"]["random_seed"]),
        "order": [str(row["treatment"]) for row in ordered],
        "default_throughput_tps": default_tps,
        "default_p99_ms": default_p99,
        "h25_throughput_tps": h25_tps,
        "h25_p99_ms": h25_p99,
        "champion_throughput_tps": champion_tps,
        "champion_p99_ms": champion_p99,
        "h25_relative_tps_percent": h25_gain,
        "h25_p99_delta_ms": h25_p99_delta,
        "champion_relative_tps_percent": champion_gain,
        "champion_p99_delta_ms": champion_p99_delta,
        "h25_vs_champion_tps_percent": (h25_tps / champion_tps - 1.0) * 100.0,
        "h25_vs_champion_p99_delta_ms": h25_p99 - champion_p99,
        "share_of_champion_tps_gain": _share(h25_gain, champion_gain),
        "share_of_champion_p99_gain": _share(h25_p99_delta, champion_p99_delta),
    }


def _same_seed_context(
    contrast: dict[str, Any], f4_rows: dict[str, dict[str, Any]], f4_champion: str
) -> dict[str, Any]:
    f4_default_tps = _optional_float(f4_rows.get("DEFAULT"), "throughput_tps")
    f4_champion_tps = _optional_float(f4_rows.get(f4_champion), "throughput_tps")
    return {
        "repetition_block": contrast["repetition_block"],
        "random_seed": contrast["random_seed"],
        "f4_default_throughput_tps": f4_default_tps,
        "f4_default_p99_ms": _optional_float(f4_rows.get("DEFAULT"), "p99_ms"),
        "f4_champion_throughput_tps": f4_champion_tps,
        "f4_champion_p99_ms": _optional_float(f4_rows.get(f4_champion), "p99_ms"),
        "block_default_throughput_tps": contrast["default_throughput_tps"],
        "block_champion_throughput_tps": contrast["champion_throughput_tps"],
        "default_drift_percent_vs_f4": _relative_percent(
            contrast["default_throughput_tps"], f4_default_tps
        ),
        "champion_drift_percent_vs_f4": _relative_percent(
            contrast["champion_throughput_tps"], f4_champion_tps
        ),
        "contemporaneous": False,
    }


def _mean(summaries: dict[str, dict[str, Any]], name: str) -> float | None:
    summary = summaries.get(name)
    return float(summary["mean"]) if summary is not None else None


def _difference(value: float | None, reference: float | None) -> float | None:
    if value is None or reference is None:
        return None
    return value - reference


def analyze_h25_observations(
    rows: list[dict[str, Any]],
    failed_attempts: list[dict[str, Any]],
    payload: dict[str, Any],
    f4_analysis: dict[str, Any],
) -> dict[str, Any]:
    if len(rows) != H25_OBSERVATIONS or not all(row.get("authenticated") is True for row in rows):
        raise ValueError("H25 analysis requires 12 authenticated terminal slots")
    by_block = {
        block: sorted(
            (row for row in rows if int(row["repetition_block"]) == block),
            key=lambda row: int(row["within_block_position"]),
        )
        for block in range(1, H25_BLOCKS + 1)
    }
    if any(
        len(block_rows) != H25_PER_BLOCK
        or {str(row["treatment"]) for row in block_rows} != set(H25_TREATMENTS)
        or str(block_rows[H25_CENTER_POSITION - 1]["treatment"]) != "DEFAULT"
        for block_rows in by_block.values()
    ):
        raise ValueError("H25 analysis requires four complete H25/DEFAULT/E blocks")
    all_valid = all(row.get("valid") is True for row in rows)
    f4_champion = str(f4_analysis.get("selected_treatment") or H25_CHAMPION)
    finalists = {
        str(row["treatment"]): {
            "mean_control_relative_tps_percent": float(row["control_relative_tps_percent"]["mean"]),
            "mean_control_relative_p99_ms": float(row["control_relative_p99_ms"]["mean"]),
        }
        for row in f4_analysis.get("candidate_summaries", [])
    }
    f4_by_seed: dict[int, dict[str, dict[str, Any]]] = {}
    for observation in f4_analysis.get("observations", []):
        f4_by_seed.setdefault(int(observation["random_seed"]), {})[
            str(observation["treatment"])
        ] = dict(observation)
    contrasts: list[dict[str, Any]] = []
    same_seed: list[dict[str, Any]] = []
    summaries: dict[str, dict[str, Any]] = {}
    if all_valid:
        for block, block_rows in by_block.items():
            contrast = _block_contrast(block, block_rows)
            contrasts.append(contrast)
            same_seed.append(
                _same_seed_context(
                    contrast, f4_by_seed.get(int(contrast["random_seed"]), {}), f4_champion
                )
            )
        for index, (name, key) in enumerate(SUMMARY_METRICS):
            values = [float(row[key]) for row in contrasts if row[key] is not None]
            if values:
                summaries[name] = _summary(values, 20260921 + index)
    f4_champion_means = finalists.get(f4_champion, {})
    mean_h25_gain = _mean(summaries, "h25_control_relative_tps_percent")
    mean_champion_gain = _mean(summaries, "champion_control_relative_tps_percent")
    mean_h25_p99 = _mean(summaries, "h25_control_relative_p99_ms")
    mean_champion_p99 = _mean(summaries, "champion_control_relative_p99_ms")
    champion_source = next(
        (str(row["source_primary_run_id"]) for row in rows if row["treatment"] == H25_CHAMPION),
        None,
    )
    successful_seconds = sum(float(row.get("lifecycle_seconds") or 0.0) for row in rows)
    failed_seconds = sum(float(row.get("lifecycle_seconds") or 0.0) for row in failed_attempts)
    return {
        "decision": "H25_MEASURED" if all_valid else "H25_INCOMPLETE",
        "evidence_role": H25_EVIDENCE_ROLE,
        "stage": H25_STAGE,
        "design_kind": H25_DESIGN_KIND,
        "all_twelve_observations_valid": all_valid,
        "treatment_configurations": {
            label: _configuration(
                next(row["requested_configuration"] for row in rows if row["treatment"] == label)
            )
            for label in H25_TREATMENTS
        },
        "block_contrasts": contrasts,
        "summaries": summaries,
        "champion_context": {
            "champion_treatment": H25_CHAMPION,
            "source_primary_run_id": champion_source,
            "contemporaneous": {
                "mean_h25_control_relative_tps_percent": mean_h25_gain,
                "mean_champion_control_relative_tps_percent": mean_champion_gain,
                "mean_h25_control_relative_p99_ms": mean_h25_p99,
                "mean_champion_control_relative_p99_ms": mean_champion_p99,
                "share_of_champion_tps_gain_ratio_of_means": _share(
                    mean_h25_gain, mean_champion_gain
                ),
                "share_of_champion_p99_gain_ratio_of_means": _share(
                    mean_h25_p99, mean_champion_p99
                ),
            },
            "f4_reference": {
                "f4_analysis_sha256": _canonical_sha256(f4_analysis),
                "f4_decision": f4_analysis.get("decision"),
                "f4_selected_treatment": f4_analysis.get("selected_treatment"),
                "finalist_control_relative_means": finalists,
                "champion_f4_mean_control_relative_tps_percent": f4_champion_means.get(
                    "mean_control_relative_tps_percent"
                ),
                "champion_f4_mean_control_relative_p99_ms": f4_champion_means.get(
                    "mean_control_relative_p99_ms"
                ),
                "champion_effect_shift_tps_percentage_points": _difference(
                    mean_champion_gain, f4_champion_means.get("mean_control_relative_tps_percent")
                ),
                "champion_effect_shift_p99_ms": _difference(
                    mean_champion_p99, f4_champion_means.get("mean_control_relative_p99_ms")
                ),
                "contemporaneous": False,
            },
        },
        "same_seed_context": same_seed,
        "observations": [
            {
                "physical_position": int(row["physical_position"]),
                "repetition_block": int(row["repetition_block"]),
                "within_block_position": int(row["within_block_position"]),
                "treatment": str(row["treatment"]),
                "source_primary_run_id": str(row.get("source_primary_run_id") or ""),
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
        },
        "time": {
            "successful_lifecycle_seconds": successful_seconds,
            "retained_failed_infrastructure_seconds": failed_seconds,
            "total_lifecycle_seconds": successful_seconds + failed_seconds,
        },
        "question": dict(payload["question"]),
        "inference_limits": {
            "independent_blocks": H25_BLOCKS,
            "descriptive_only": True,
            "formal_superiority_claim_authorized": False,
            "champion_selection_unchanged": True,
            "apply_best_unchanged": True,
            "f4_context_is_non_contemporaneous": True,
        },
    }


def analyze_h25(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    block = _h25_block(settings, campaign_id)
    if block["status"] == "ANALYZED":
        analysis = dict(block["analysis"] or {})
        if _canonical_sha256(analysis) != str(block["analysis_sha256"]):
            raise ValueError("H25 durable analysis hash mismatch")
        return {**analysis, "analysis_sha256": str(block["analysis_sha256"])}
    rows, failed_attempts = _load_authenticated_rows(settings, campaign_id)
    _, payload = h25_manifest_payload()
    f4 = _f4_block(settings, uuid.UUID(str(block["f4_campaign_id"])))
    f4_analysis = dict(f4["analysis"] or {})
    if str(f4["f4_block_id"]) != str(block["f4_block_id"]) or _canonical_sha256(f4_analysis) != str(
        block["f4_analysis_sha256"]
    ):
        raise ValueError("H25 source F4 analysis does not match the recorded lineage")
    analysis = analyze_h25_observations(rows, failed_attempts, payload, f4_analysis)
    analysis_sha256 = _canonical_sha256(analysis)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_h25_blocks
               SET status='ANALYZED',analysis=%s,analysis_sha256=%s,completed_at=clock_timestamp()
               WHERE h25_block_id=%s AND status='OBSERVATIONS_COMPLETE'""",
            (Jsonb(analysis), analysis_sha256, block["h25_block_id"]),
        )
        if cur.rowcount != 1:
            raise RuntimeError("H25 block lost its observations-complete state")
        conn.commit()
    control_campaign(
        settings,
        campaign_id,
        "stop",
        f"H25 analysis complete: {analysis['decision']}",
        actor=H25_STAGE,
    )
    return {**analysis, "analysis_sha256": analysis_sha256}


def export_h25_analysis(
    settings: Settings, campaign_id: uuid.UUID, output_path: Path | None = None
) -> dict[str, Any]:
    block = _h25_block(settings, campaign_id)
    if block["status"] != "ANALYZED" or block["analysis"] is None:
        raise ValueError("H25 export requires a terminal analyzed block")
    root = (settings.artifact_dir / H25_STAGE).resolve()
    output = (output_path or root / "h25-analysis.json").resolve()
    if output != root and root not in output.parents:
        raise ValueError("H25 export must stay under its artifact root")
    payload = {
        "campaign_id": str(campaign_id),
        "h25_block_id": str(block["h25_block_id"]),
        "design_kind": block.get("design_kind"),
        "supersedes_h25_block_id": (
            str(block["supersedes_h25_block_id"]) if block.get("supersedes_h25_block_id") else None
        ),
        "manifest_sha256": str(block["manifest_sha256"]),
        "f4_campaign_id": str(block["f4_campaign_id"]),
        "f4_analysis_sha256": str(block["f4_analysis_sha256"]),
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


def _summary_rows(summaries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for metric, summary in summaries.items():
        low, high = summary["bootstrap_ci_95"]
        rows.append(
            {
                "metric": metric,
                "n": summary["n"],
                "mean": summary["mean"],
                "median": summary["median"],
                "standard_deviation": summary["standard_deviation"],
                "median_absolute_deviation": summary["median_absolute_deviation"],
                "ci_low": low,
                "ci_high": high,
            }
        )
    return rows


def _format_summary(summary: dict[str, Any] | None, unit: str) -> str:
    if summary is None:
        return "not available"
    low, high = summary["bootstrap_ci_95"]
    return f"{float(summary['mean']):.3f}{unit} (bootstrap 95% CI {low:.3f} to {high:.3f})"


def _format_optional(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _report_markdown(analysis: dict[str, Any]) -> str:
    summaries = dict(analysis["summaries"])
    context = dict(analysis["champion_context"])
    contemporaneous = dict(context["contemporaneous"])
    reference = dict(context["f4_reference"])
    tps_share = _format_optional(contemporaneous["share_of_champion_tps_gain_ratio_of_means"])
    p99_share = _format_optional(contemporaneous["share_of_champion_p99_gain_ratio_of_means"])
    f4_tps = _format_optional(reference["champion_f4_mean_control_relative_tps_percent"])
    f4_p99 = _format_optional(reference["champion_f4_mean_control_relative_p99_ms"])
    shift = _format_optional(reference["champion_effect_shift_tps_percentage_points"])
    lines = [
        "# H25 heuristic-baseline report",
        "",
        f"Decision: **{analysis['decision']}**",
        "",
        f"Question: {analysis['question']['statement']}",
        "",
        "Design: four restored common-seed blocks on the F4 seeds. Each block measures H25 "
        "(frozen default with shared_buffers = 1 GiB), the frozen DEFAULT at the center "
        "position, and F4 champion E, with the outer order counterbalanced across blocks.",
        "",
        f"Valid observations: {analysis['counts']['valid_slots']}/{H25_OBSERVATIONS}.",
        "",
        "Mean same-block H25 change versus DEFAULT: TPS "
        f"{_format_summary(summaries.get('h25_control_relative_tps_percent'), '%')}; p99 "
        f"{_format_summary(summaries.get('h25_control_relative_p99_ms'), ' ms')}.",
        "",
        "Mean same-block champion E change versus DEFAULT: TPS "
        f"{_format_summary(summaries.get('champion_control_relative_tps_percent'), '%')}; p99 "
        f"{_format_summary(summaries.get('champion_control_relative_p99_ms'), ' ms')}.",
        "",
        "Share of the champion's same-block TPS gain recovered by shared_buffers alone: "
        f"ratio of means {tps_share}; per-block "
        f"{_format_summary(summaries.get('share_of_champion_tps_gain'), '')}. "
        f"Share of the champion's p99 improvement: ratio of means {p99_share}; per-block "
        f"{_format_summary(summaries.get('share_of_champion_p99_gain'), '')}.",
        "",
        "Direct H25 versus champion E in the same block: TPS "
        f"{_format_summary(summaries.get('h25_vs_champion_tps_percent'), '%')}; p99 "
        f"{_format_summary(summaries.get('h25_vs_champion_p99_ms'), ' ms')}.",
        "",
        "| Block | Seed | Order | DEFAULT TPS | H25 TPS | E TPS | H25 % | E % | TPS share "
        "| DEFAULT p99 | H25 p99 | E p99 | p99 share |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in analysis["block_contrasts"]:
        lines.append(
            f"| {row['repetition_block']} | {row['random_seed']} | {'-'.join(row['order'])} "
            f"| {row['default_throughput_tps']:.3f} | {row['h25_throughput_tps']:.3f} "
            f"| {row['champion_throughput_tps']:.3f} | {row['h25_relative_tps_percent']:.3f} "
            f"| {row['champion_relative_tps_percent']:.3f} "
            f"| {_format_optional(row['share_of_champion_tps_gain'])} "
            f"| {row['default_p99_ms']:.3f} | {row['h25_p99_ms']:.3f} "
            f"| {row['champion_p99_ms']:.3f} "
            f"| {_format_optional(row['share_of_champion_p99_gain'])} |"
        )
    lines.extend(
        [
            "",
            "F4 reference (not contemporaneous; F4 ran earlier): the F4 mean same-block TPS gain "
            f"of champion E was {f4_tps}% and its p99 change {f4_p99} ms; in this block the "
            f"champion's mean TPS gain differs from F4 by {shift} percentage points.",
            "",
            "| Block | F4 DEFAULT TPS | Block DEFAULT TPS | DEFAULT drift % | F4 E TPS "
            "| Block E TPS | E drift % |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in analysis["same_seed_context"]:
        lines.append(
            f"| {row['repetition_block']} | {_format_optional(row['f4_default_throughput_tps'])} "
            f"| {row['block_default_throughput_tps']:.3f} "
            f"| {_format_optional(row['default_drift_percent_vs_f4'])} "
            f"| {_format_optional(row['f4_champion_throughput_tps'])} "
            f"| {row['block_champion_throughput_tps']:.3f} "
            f"| {_format_optional(row['champion_drift_percent_vs_f4'])} |"
        )
    lines.extend(
        [
            "",
            "Four restored common-seed blocks are the independent units. This is a descriptive "
            "supplement: no formal superiority claim is made, and neither the F4 champion "
            "selection nor the apply-best deployment decision changes.",
            "",
        ]
    )
    return "\n".join(lines)


def render_h25_report(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    block = _h25_block(settings, campaign_id)
    if block["status"] != "ANALYZED" or block["analysis"] is None:
        raise ValueError("H25 report requires a terminal analyzed block")
    analysis = dict(block["analysis"])
    root = (settings.artifact_dir / H25_STAGE / "report").resolve()
    root.mkdir(parents=True, exist_ok=True)
    contrasts = [
        {**contrast, **{key: value for key, value in context.items() if key not in contrast}}
        for contrast, context in zip(
            analysis["block_contrasts"], analysis["same_seed_context"], strict=True
        )
    ]
    files: list[dict[str, Any]] = []
    content = {
        "tables/h25-observations.csv": _csv_bytes(list(analysis["observations"])),
        "tables/h25-summary.csv": _csv_bytes(_summary_rows(dict(analysis["summaries"]))),
        "tables/h25-block-contrasts.csv": _csv_bytes(contrasts),
        "h25-report.md": _report_markdown(analysis).encode(),
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
    index_path = root / "h25-report-index.json"
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    return {
        "report_root": str(root),
        "index_path": str(index_path),
        "index_file_sha256": _file_sha256(index_path),
        "file_count": len(files) + 1,
    }


def h25_step_dict(step: H25Step) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(json.dumps(asdict(step), default=str)))
