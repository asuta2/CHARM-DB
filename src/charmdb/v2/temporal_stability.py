from __future__ import annotations

import hashlib
import json
import uuid
from itertools import pairwise
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.controller import discover_knobs, validate_candidate
from charmdb.db import connect
from charmdb.manifest_preflight import _target_safety
from charmdb.v2.default_reference import (
    FROZEN_BASELINE_ID,
    FROZEN_CLIENT_THREADS,
    FROZEN_CONCURRENCY,
    FROZEN_MAINTENANCE_POLICY,
    FROZEN_MEASUREMENT_SECONDS,
    FROZEN_PREFLIGHT_ID,
    FROZEN_PROFILE_ID,
    FROZEN_RESTORE_MECHANISM,
    FROZEN_WARMUP_SECONDS,
    DefaultReferenceStep,
    _default_configuration,
    _json_sha256,
    analyze_reference_points,
    default_reference_history,
    run_default_reference_next,
)
from charmdb.v2.protocol import load_manifest
from charmdb.worker import control_campaign, create_campaign

TEMPORAL_STABILITY_MANIFEST = Path("v2/config/post-screening-temporal-stability.json")
TEMPORAL_STABILITY_STAGE = "post-screening-temporal-stability"
SOURCE_CAMPAIGN_ID = uuid.UUID("8565ecfc-a584-46a9-a83f-d4347dbc1eb1")
SOURCE_RECOVERY_BLOCK_ID = uuid.UUID("374f0641-ada0-53cf-bc35-d20e1fdfe64e")
SOURCE_ANALYSIS_SHA256 = "5a51b941f1511b8f55962a3e5fcf279c71b98e89e94e72db4d5b7b7b2b82b1dd"
SEED_LABEL_PREFIX = "thesis-protocol-v2/post-screening-temporal-stability"
PRIMARY_SEEDS = (1055524210, 1566314102, 1188081992, 1692640140, 1481021899)
RESERVED_SEEDS = (761422838, 1922024623, 1023249220, 848330793, 1947955481)
MAXIMUM_OPERATOR_GAP_MINUTES = 15.0


def _derived_seed(index: int) -> int:
    label = f"{SEED_LABEL_PREFIX}/{index}"
    return int.from_bytes(hashlib.sha256(label.encode("utf-8")).digest()[:4], "big") % 2_147_483_647


def temporal_stability_manifest_payload(
    path: Path = TEMPORAL_STABILITY_MANIFEST,
) -> tuple[str, dict[str, Any]]:
    manifest = load_manifest(path)
    if manifest.stage != TEMPORAL_STABILITY_STAGE or manifest.evidence_role != "CALIBRATION":
        raise ValueError("temporal stability requires its dedicated CALIBRATION manifest")
    payload = manifest.payload
    source = dict(payload.get("source") or {})
    if (
        source.get("campaign_id") != str(SOURCE_CAMPAIGN_ID)
        or source.get("recovery_block_id") != str(SOURCE_RECOVERY_BLOCK_ID)
        or source.get("required_block_status") != "BLOCKED"
        or source.get("required_campaign_status") != "STOPPED"
        or source.get("required_outcome") != "BLOCKED_DRIFT"
        or source.get("analysis_sha256") != SOURCE_ANALYSIS_SHA256
    ):
        raise ValueError("temporal-stability source differs from terminal D033")
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
    if profile != expected_profile:
        raise ValueError("temporal-stability benchmark profile differs from the frozen profile")
    purpose = dict(payload.get("purpose") or {})
    if (
        purpose.get("does_not_salvage_or_reinterpret_d033") is not True
        or purpose.get("does_not_authorize_primary_comparison") is not True
    ):
        raise ValueError("temporal-stability purpose must preserve the D033 block")
    design = dict(payload.get("design") or {})
    derived_primary = tuple(_derived_seed(index) for index in range(1, 6))
    derived_reserved = tuple(_derived_seed(index) for index in range(6, 11))
    if (
        design.get("kind") != "single-fixed-five-default-launch-condition-block"
        or design.get("observations") != 5
        or design.get("configuration") != "postgresql_default"
        or design.get("maximum_inter_observation_operator_gap_minutes")
        != MAXIMUM_OPERATOR_GAP_MINUTES
        or design.get("no_contingency_or_extension") is not True
        or tuple(design.get("seeds") or ()) != PRIMARY_SEEDS
        or tuple(design.get("reserved_unusable_ledger_seeds") or ()) != RESERVED_SEEDS
        or derived_primary != PRIMARY_SEEDS
        or derived_reserved != RESERVED_SEEDS
    ):
        raise ValueError("temporal-stability design or fresh seeds differ from D034")
    criteria = dict(payload.get("acceptance_criteria") or {})
    expected_criteria = {
        "confidence_level": 0.95,
        "maximum_tps_relative_ci_half_width": 0.05,
        "maximum_p99_ci_half_width_ms": 5.0,
        "maximum_tps_fitted_block_change_relative": 0.05,
        "maximum_p99_fitted_block_change_ms": 5.0,
        "required_valid_observations": 5,
        "maximum_total_observations": 5,
        "benchmark_failures_max": 0,
    }
    if criteria != expected_criteria:
        raise ValueError("temporal-stability criteria differ from D034")
    runtime = dict(payload.get("runtime") or {})
    if (
        runtime.get("observations") != 5
        or runtime.get("restore_inclusive_hours") != 2.311
        or runtime.get("accepted") is not True
        or runtime.get("accepted_by") != "D034"
    ):
        raise ValueError("temporal-stability runtime differs from D034")
    return hashlib.sha256(path.read_bytes()).hexdigest(), payload


def temporal_stability_design_summary(
    path: Path = TEMPORAL_STABILITY_MANIFEST,
) -> dict[str, Any]:
    manifest_sha256, payload = temporal_stability_manifest_payload(path)
    return {
        "status": payload["status"],
        "execution_ready": payload["execution_ready"],
        "retirement": payload["retirement"],
        "manifest_sha256": manifest_sha256,
        "source_analysis_sha256": payload["source"]["analysis_sha256"],
        "observations": payload["design"]["observations"],
        "seeds": payload["design"]["seeds"],
        "maximum_operator_gap_minutes": payload["design"][
            "maximum_inter_observation_operator_gap_minutes"
        ],
        "restore_inclusive_hours": payload["runtime"]["restore_inclusive_hours"],
        "unresolved_decisions": payload["unresolved_decisions"],
    }


def _source_state(settings: Settings) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT b.recovery_block_id,b.campaign_id,b.status,b.final_analysis,
                      b.final_analysis_sha256,c.status AS campaign_status
               FROM charm_control.experiment_v2_screening_recovery_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.recovery_block_id=%s AND b.campaign_id=%s""",
            (SOURCE_RECOVERY_BLOCK_ID, SOURCE_CAMPAIGN_ID),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError("D034 requires the terminal D033 screening-recovery block")
    analysis = dict(row["final_analysis"] or {})
    if (
        row["status"] != "BLOCKED"
        or row["campaign_status"] != "STOPPED"
        or row["final_analysis_sha256"] != SOURCE_ANALYSIS_SHA256
        or analysis.get("outcome") != "BLOCKED_DRIFT"
        or analysis.get("analysis_sha256") != SOURCE_ANALYSIS_SHA256
    ):
        raise ValueError("D033 source state differs from the D034 pre-registration")
    return {**dict(row), "final_analysis": analysis}


def temporal_stability_readiness(
    settings: Settings,
    manifest_path: Path = TEMPORAL_STABILITY_MANIFEST,
) -> dict[str, Any]:
    manifest_sha256, payload = temporal_stability_manifest_payload(manifest_path)
    retirement = dict(payload.get("retirement") or {})
    if retirement.get("permanently_prohibit_launch") is True:
        return {
            "ready": False,
            "retired": True,
            "decision": retirement.get("decision"),
            "reason": retirement.get("reason"),
            "manifest_sha256": manifest_sha256,
            "source_recovery_block_id": str(SOURCE_RECOVERY_BLOCK_ID),
            "source_analysis_sha256": SOURCE_ANALYSIS_SHA256,
            "observations_executed": retirement.get("observations_executed"),
        }
    source = _source_state(settings)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT preflight_id,restore_mechanism,approved
               FROM charm_control.experiment_candidate_dataset_baselines
               WHERE baseline_id=%s""",
            (FROZEN_BASELINE_ID,),
        )
        baseline = cur.fetchone()
        cur.execute(
            """SELECT block_id,status,campaign_id
               FROM charm_control.experiment_v2_default_reference_blocks
               WHERE source_screening_recovery_block_id=%s""",
            (SOURCE_RECOVERY_BLOCK_ID,),
        )
        existing = cur.fetchone()
    if (
        baseline is None
        or baseline["approved"] is not True
        or uuid.UUID(str(baseline["preflight_id"])) != FROZEN_PREFLIGHT_ID
        or baseline["restore_mechanism"] != FROZEN_RESTORE_MECHANISM
    ):
        raise ValueError("D034 requires the approved frozen logical-restore baseline")
    target = _target_safety(settings)
    configuration = _default_configuration()
    metadata = discover_knobs(settings, set(configuration))
    validate_candidate(configuration, metadata)
    boot = {str(row["name"]): str(row["boot_val"]) for row in metadata}
    if configuration != boot:
        raise ValueError("D034 default vector differs from PostgreSQL boot defaults")
    return {
        "ready": (
            payload["status"] == "ready"
            and payload["execution_ready"] is True
            and source["status"] == "BLOCKED"
            and target["active_campaigns"] == 0
            and existing is None
        ),
        "manifest_sha256": manifest_sha256,
        "source_recovery_block_id": str(SOURCE_RECOVERY_BLOCK_ID),
        "source_analysis_sha256": SOURCE_ANALYSIS_SHA256,
        "baseline_id": str(FROZEN_BASELINE_ID),
        "benchmark_profile_id": FROZEN_PROFILE_ID,
        "observations": 5,
        "maximum_operator_gap_minutes": MAXIMUM_OPERATOR_GAP_MINUTES,
        "existing_block": dict(existing) if existing is not None else None,
        "target_safety": target,
    }


def create_temporal_stability_plan(
    settings: Settings,
    manifest_path: Path = TEMPORAL_STABILITY_MANIFEST,
) -> uuid.UUID:
    _, payload = temporal_stability_manifest_payload(manifest_path)
    if payload["retirement"]["permanently_prohibit_launch"] is True:
        raise ValueError("D034 was retired without execution by D035 and cannot be launched")
    readiness = temporal_stability_readiness(settings, manifest_path)
    if readiness["ready"] is not True:
        raise ValueError("temporal-stability execution is blocked until every gate passes")
    _, payload = temporal_stability_manifest_payload(manifest_path)
    campaign_id = create_campaign(
        settings,
        "v2-post-screening-temporal-stability",
        "CALIBRATION",
        {"maximize": "throughput_tps", "minimize": "p99_ms"},
        {"failures": 0, "p99_slo_applied": False},
        failure_limit=1,
        campaign_settings={
            "protocol_id": "thesis-protocol-v2",
            "stage": TEMPORAL_STABILITY_STAGE,
            "manifest_sha256": readiness["manifest_sha256"],
            "source_screening_recovery_block_id": str(SOURCE_RECOVERY_BLOCK_ID),
            "source_analysis_sha256": SOURCE_ANALYSIS_SHA256,
            "preflight_id": str(FROZEN_PREFLIGHT_ID),
            "candidate_restore_baseline_id": str(FROZEN_BASELINE_ID),
            "restore_mechanism": FROZEN_RESTORE_MECHANISM,
            "benchmark_profile_id": FROZEN_PROFILE_ID,
            "manifest": payload,
        },
        actor="v2-temporal-stability",
    )
    block_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{campaign_id}:v2-temporal-stability")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_default_reference_blocks
               (block_id,campaign_id,protocol_id,evidence_role,manifest_sha256,
                preflight_id,baseline_id,benchmark_profile_id,status,primary_seeds,
                contingency_seeds,acceptance_criteria,
                source_screening_recovery_block_id,qualification_purpose)
               VALUES (%s,%s,'thesis-protocol-v2','CALIBRATION',%s,%s,%s,%s,
                       'INITIAL_PLANNED',%s,%s,%s,%s,
                       'POST_SCREENING_TEMPORAL_STABILITY')""",
            (
                block_id,
                campaign_id,
                readiness["manifest_sha256"],
                FROZEN_PREFLIGHT_ID,
                FROZEN_BASELINE_ID,
                FROZEN_PROFILE_ID,
                Jsonb(list(PRIMARY_SEEDS)),
                Jsonb(list(RESERVED_SEEDS)),
                Jsonb(payload["acceptance_criteria"]),
                SOURCE_RECOVERY_BLOCK_ID,
            ),
        )
        for sequence, seed in enumerate(PRIMARY_SEEDS, start=1):
            run_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"charmdb:{block_id}:temporal-stability:{sequence}:{seed}",
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


def temporal_stability_history(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    history = default_reference_history(settings, campaign_id)
    block = history["block"]
    if block.get("qualification_purpose") != "POST_SCREENING_TEMPORAL_STABILITY" or str(
        block.get("source_screening_recovery_block_id")
    ) != str(SOURCE_RECOVERY_BLOCK_ID):
        raise ValueError("campaign is not the D034 temporal-stability block")
    return history


def run_temporal_stability_next(
    settings: Settings,
    campaign_id: uuid.UUID,
    *,
    owner: str = "v2-temporal-stability",
    lease_seconds: int = 600,
) -> DefaultReferenceStep:
    temporal_stability_history(settings, campaign_id)
    return run_default_reference_next(
        settings, campaign_id, owner=owner, lease_seconds=lease_seconds
    )


def _operator_gap_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: int(row["chronological_execution_index"]))
    gaps: list[dict[str, Any]] = []
    for previous, current in pairwise(ordered):
        completed = previous.get("trial_completed_at")
        started = current.get("trial_started_at")
        if completed is None or started is None:
            raise ValueError("temporal-stability gap analysis requires complete timestamps")
        minutes = (started - completed).total_seconds() / 60.0
        gaps.append(
            {
                "after_sequence": int(previous["sequence"]),
                "before_sequence": int(current["sequence"]),
                "minutes": minutes,
            }
        )
    maximum = max((float(item["minutes"]) for item in gaps), default=0.0)
    return {
        "maximum_allowed_minutes": MAXIMUM_OPERATOR_GAP_MINUTES,
        "maximum_observed_minutes": maximum,
        "passed": maximum <= MAXIMUM_OPERATOR_GAP_MINUTES,
        "gaps": gaps,
    }


def analyze_temporal_stability(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    history = temporal_stability_history(settings, campaign_id)
    block = history["block"]
    if block["campaign_status"] != "PAUSED" or block["status"] != "INITIAL_COMPLETE":
        raise ValueError("temporal-stability analysis requires a clean completed paused block")
    manifest_sha256, payload = temporal_stability_manifest_payload()
    if block["manifest_sha256"] != manifest_sha256:
        raise ValueError("temporal-stability manifest differs from the frozen campaign")
    rows = [row for row in history["runs"] if row["status"] == "COMPLETED"]
    if len(rows) != 5:
        raise ValueError("temporal-stability analysis requires five completed observations")
    points: list[dict[str, Any]] = []
    for row in rows:
        objectives = dict(row.get("objective_values") or {})
        constraints = dict(row.get("constraint_values") or {})
        points.append(
            {
                "run_id": str(row["run_id"]),
                "trial_id": str(row["trial_id"]),
                "random_seed": int(row["random_seed"]),
                "chronological_execution_index": int(row["chronological_execution_index"]),
                "throughput_tps": float(objectives["throughput_tps"]),
                "p99_ms": float(objectives["p99_ms"]),
                "failures": int(constraints.get("failures", 0)),
                "restore_id": str(row["restore_id"]),
                "restore_seconds": float(row["restore_seconds"]),
                "exact_core_passed": row["exact_core_passed"],
                "physical_statistics_passed": row["physical_statistics_passed"],
                "total_seconds": float(row["total_seconds"]),
            }
        )
    base = analyze_reference_points(points, dict(block["acceptance_criteria"]))
    gaps = _operator_gap_summary(rows)
    outcome = str(base["outcome"])
    if gaps["passed"] is not True:
        outcome = "BLOCKED_OPERATOR_GAP"
    elif outcome == "CONTINGENCY_REQUIRED":
        outcome = "BLOCKED_IMPRECISION"
    analysis = {
        "campaign_id": str(campaign_id),
        "block_id": str(block["block_id"]),
        "source_screening_recovery_block_id": str(SOURCE_RECOVERY_BLOCK_ID),
        "source_analysis_sha256": SOURCE_ANALYSIS_SHA256,
        "benchmark_profile_id": block["benchmark_profile_id"],
        "manifest_sha256": manifest_sha256,
        "purpose": payload["purpose"],
        "operator_gap": gaps,
        **base,
        "outcome": outcome,
    }
    analysis["analysis_sha256"] = _json_sha256(analysis)
    terminal_status = "PASSED" if outcome == "PASSED" else "BLOCKED"
    block_id = uuid.UUID(str(block["block_id"]))
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_default_reference_blocks
               SET status=%s,initial_analysis=%s,initial_analysis_sha256=%s,
                   final_analysis=%s,final_analysis_sha256=%s,
                   completed_at=clock_timestamp()
               WHERE block_id=%s AND status='INITIAL_COMPLETE'""",
            (
                terminal_status,
                Jsonb(analysis),
                analysis["analysis_sha256"],
                Jsonb(analysis),
                analysis["analysis_sha256"],
                block_id,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError("temporal-stability block lost its analyzable state")
        conn.commit()
    control_campaign(
        settings,
        campaign_id,
        "stop",
        f"post-screening temporal stability resolved as {terminal_status}",
        actor="v2-temporal-stability-analysis",
    )
    analysis["block_status"] = terminal_status
    analysis["next_action"] = (
        "pre-register one complete fresh screening replicate; primary remains blocked"
        if terminal_status == "PASSED"
        else "stop local screening continuation pending an environment or protocol change"
    )
    return analysis


def export_temporal_stability_analysis(
    settings: Settings,
    campaign_id: uuid.UUID,
    output: Path | None = None,
) -> dict[str, str]:
    history = temporal_stability_history(settings, campaign_id)
    block = history["block"]
    analysis = block.get("final_analysis")
    analysis_sha256 = block.get("final_analysis_sha256")
    if block["status"] not in {"PASSED", "BLOCKED"} or not isinstance(analysis, dict):
        raise ValueError("temporal-stability export requires a terminal analysis")
    if not isinstance(analysis_sha256, str) or len(analysis_sha256) != 64:
        raise ValueError("temporal-stability analysis has no valid SHA-256")
    hash_payload = dict(analysis)
    embedded_sha256 = hash_payload.pop("analysis_sha256", None)
    if embedded_sha256 != analysis_sha256 or _json_sha256(hash_payload) != analysis_sha256:
        raise ValueError("temporal-stability analysis hash does not verify")
    _, payload = temporal_stability_manifest_payload()
    artifact_root = Path(str(payload["artifact_root"])).resolve()
    destination = (
        output.resolve()
        if output is not None
        else (artifact_root / f"temporal-stability-analysis-{block['block_id']}.json").resolve()
    )
    try:
        destination.relative_to(artifact_root)
    except ValueError as error:
        raise ValueError("temporal-stability export must stay under its artifact root") from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(analysis, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return {
        "campaign_id": str(campaign_id),
        "block_id": str(block["block_id"]),
        "analysis_sha256": analysis_sha256,
        "output_path": str(destination),
    }
