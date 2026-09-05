from __future__ import annotations

import csv
import hashlib
import io
import json
import statistics
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.controller import discover_knobs, validate_candidate
from charmdb.db import connect
from charmdb.statistics import descriptive_summary, holm_adjust, paired_comparison
from charmdb.v2.default_reference import (
    FROZEN_BASELINE_ID,
    FROZEN_PREFLIGHT_ID,
    FROZEN_PROFILE_ID,
)
from charmdb.v2.primary_design import (
    PRIMARY_MANIFEST,
    PRIMARY_PARAMETER_ORDER,
    PRIMARY_WAVE_B_MANIFEST,
    build_wave_b_plan,
    primary_candidate_design_sha256_for_seeds,
    schedule_sha256,
)
from charmdb.v2.primary_execution import (
    _canonical_sha256,
    _derived_seed,
    _primary_block,
    _proposal_sha256,
    _run_primary_next_with_contract,
    analyze_primary_observations,
    primary_history,
    primary_manifest_payload,
    primary_readiness,
    primary_report_context,
    primary_step_dict,
)
from charmdb.v2.primary_optimizer import primary_acquisition_name
from charmdb.v2.primary_reporting import METHOD_LABELS, render_primary_report
from charmdb.v2.protocol import PRIMARY_SEARCH_METHODS, load_manifest
from charmdb.worker import control_campaign, create_campaign

WAVE_B_STAGE = "primary-wave-b"
WAVE_B_EXPECTED_OBSERVATIONS = 262
WAVE_B_SEEDS = (1902413987, 740267717)
WAVE_A_CAMPAIGN_ID = uuid.UUID("b0619879-c807-4ee3-859e-2c2dbec9934e")

METRIC_DEFINITIONS = {
    "hypervolume_at_0_negative_40": "higher-is-better",
    "best_throughput_tps": "higher-is-better",
    "minimum_p99_ms": "lower-is-better",
    "mean_control_relative_tps": "higher-is-better",
    "mean_control_relative_p99_ms": "lower-is-better",
}


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_contract(
    manifest_path: Path = PRIMARY_WAVE_B_MANIFEST,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    manifest = load_manifest(manifest_path)
    if manifest.stage != WAVE_B_STAGE or manifest.evidence_role != "PRIMARY":
        raise ValueError("Wave B requires its dedicated PRIMARY manifest")
    wave_payload = manifest.payload
    source = dict(wave_payload["source_evidence"])
    primary_path = Path(str(source["wave_a_manifest_path"]))
    primary_sha, primary_payload = primary_manifest_payload(primary_path)
    if primary_path != PRIMARY_MANIFEST or primary_sha != source["wave_a_manifest_sha256"]:
        raise ValueError("Wave B source Wave A manifest does not match D063")
    preregistration = Path(str(source["preregistration_path"]))
    if (
        not preregistration.is_file()
        or _file_sha256(preregistration) != source["preregistration_sha256"]
    ):
        raise ValueError("Wave B pre-registration file does not match D063")
    seeds = [int(seed) for seed in wave_payload["wave_b"]["seeds"]]
    plan = build_wave_b_plan(wave_payload, primary_payload)
    if (
        schedule_sha256(tuple(item.schedule for item in plan))
        != wave_payload["execution_schedule"]["schedule_sha256"]
    ):
        raise ValueError("Wave B schedule differs from its frozen SHA-256")
    if (
        primary_candidate_design_sha256_for_seeds(primary_payload, seeds)
        != wave_payload["execution_schedule"]["candidate_design_sha256"]
    ):
        raise ValueError("Wave B fixed-candidate design differs from its frozen SHA-256")
    if len(plan) != WAVE_B_EXPECTED_OBSERVATIONS:
        raise ValueError("Wave B plan must contain exactly 262 physical observations")
    return _file_sha256(manifest_path), wave_payload, primary_payload


def wave_b_design_summary(
    manifest_path: Path = PRIMARY_WAVE_B_MANIFEST,
) -> dict[str, Any]:
    manifest_sha, wave_payload, primary_payload = _load_contract(manifest_path)
    plan = build_wave_b_plan(wave_payload, primary_payload)
    method_counts: dict[str, int] = {}
    for item in plan:
        method = item.schedule.method
        method_counts[method] = method_counts.get(method, 0) + 1
    return {
        "status": wave_payload["status"],
        "execution_ready": wave_payload["execution_ready"],
        "manifest_sha256": manifest_sha,
        "wave": "B",
        "seeds": list(WAVE_B_SEEDS),
        "physical_observations": len(plan),
        "method_physical_observations": dict(sorted(method_counts.items())),
        "schedule_seed": wave_payload["execution_schedule"]["schedule_seed"],
        "schedule_sha256": wave_payload["execution_schedule"]["schedule_sha256"],
        "candidate_design_sha256": wave_payload["execution_schedule"]["candidate_design_sha256"],
        "unresolved_decisions": wave_payload["unresolved_decisions"],
    }


def export_wave_b_design(
    manifest_path: Path = PRIMARY_WAVE_B_MANIFEST,
    output_path: Path | None = None,
) -> dict[str, Any]:
    manifest_sha, wave_payload, primary_payload = _load_contract(manifest_path)
    plan = build_wave_b_plan(wave_payload, primary_payload)
    artifact_root = Path(load_manifest(manifest_path).artifact_root).resolve()
    output = (output_path or artifact_root / "primary-wave-b-design.json").resolve()
    if output != artifact_root and artifact_root not in output.parents:
        raise ValueError("Wave B design export must stay under its artifact root")
    payload = {
        "protocol_id": "thesis-protocol-v2",
        "stage": WAVE_B_STAGE,
        "wave": "B",
        "manifest_sha256": manifest_sha,
        "schedule_sha256": wave_payload["execution_schedule"]["schedule_sha256"],
        "candidate_design_sha256": wave_payload["execution_schedule"]["candidate_design_sha256"],
        "entries": [
            {
                "global_position": item.schedule.global_position,
                "seed_index": item.schedule.seed_index,
                "seed": item.schedule.seed,
                "within_seed_position": item.schedule.within_seed_position,
                "evaluation_role": item.schedule.evaluation_role,
                "method": item.schedule.method,
                "budget_position": item.schedule.budget_position,
                "shared_with_methods": list(item.schedule.shared_with_methods),
                "candidate_vector": list(item.candidate.vector) if item.candidate else None,
                "requested_configuration": (
                    item.candidate.configuration if item.candidate else None
                ),
            }
            for item in plan
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {
        "output_path": str(output),
        "file_sha256": _file_sha256(output),
        "manifest_sha256": manifest_sha,
        "schedule_sha256": payload["schedule_sha256"],
        "candidate_design_sha256": payload["candidate_design_sha256"],
    }


def wave_b_readiness(
    settings: Settings,
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest_path: Path = PRIMARY_WAVE_B_MANIFEST,
) -> dict[str, Any]:
    manifest_sha, wave_payload, primary_payload = _load_contract(manifest_path)
    base = primary_readiness(settings, preflight_id, PRIMARY_MANIFEST)
    blockers = [
        str(item) for item in base["blockers"] if str(item) != "primary-block-already-exists"
    ]
    source = dict(wave_payload["source_evidence"])
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT EXISTS (
                   SELECT 1 FROM information_schema.columns
                   WHERE table_schema='charm_control'
                     AND table_name='experiment_v2_primary_blocks'
                     AND column_name='wave'
               ) AS installed"""
        )
        migration_row = cur.fetchone()
        migration_installed = bool(migration_row and migration_row["installed"])
        cur.execute(
            """SELECT b.primary_block_id,b.status,b.analysis_sha256,
                      c.campaign_id,c.status AS campaign_status
               FROM charm_control.experiment_v2_primary_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.primary_block_id=%s AND b.campaign_id=%s""",
            (source["wave_a_primary_block_id"], source["wave_a_campaign_id"]),
        )
        wave_a = cur.fetchone()
        if migration_installed:
            cur.execute(
                """SELECT b.primary_block_id,b.status,c.campaign_id,
                          c.status AS campaign_status
                   FROM charm_control.experiment_v2_primary_blocks b
                   JOIN charm_control.campaigns c USING(campaign_id)
                   WHERE b.wave='B'"""
            )
            existing_wave_b = cur.fetchone()
        else:
            existing_wave_b = None
    if not migration_installed:
        blockers.append("migration-041-not-applied")
    if (
        wave_a is None
        or str(wave_a["status"]) != "ANALYZED"
        or str(wave_a["campaign_status"]) != "STOPPED"
        or str(wave_a["analysis_sha256"]) != source["wave_a_analysis_payload_sha256"]
    ):
        blockers.append("wave-a-terminal-analysis-not-authenticated")
    if existing_wave_b is not None:
        blockers.append("wave-b-block-already-exists")
    if wave_payload["status"] != "ready" or wave_payload["execution_ready"] is not True:
        blockers.append("wave-b-manifest-not-ready")

    metadata = discover_knobs(settings, set(PRIMARY_PARAMETER_ORDER))
    for item in build_wave_b_plan(wave_payload, primary_payload):
        if item.candidate is not None:
            validate_candidate(item.candidate.configuration, metadata)
    return {
        "ready": not blockers,
        "blockers": blockers,
        "manifest_sha256": manifest_sha,
        "wave": "B",
        "seeds": list(WAVE_B_SEEDS),
        "physical_observations": WAVE_B_EXPECTED_OBSERVATIONS,
        "schedule_sha256": wave_payload["execution_schedule"]["schedule_sha256"],
        "candidate_design_sha256": wave_payload["execution_schedule"]["candidate_design_sha256"],
        "base_primary_readiness": base,
        "migration_041_installed": migration_installed,
        "wave_a_source": dict(wave_a) if wave_a is not None else None,
        "existing_wave_b": dict(existing_wave_b) if existing_wave_b is not None else None,
    }


def create_wave_b_plan(
    settings: Settings,
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest_path: Path = PRIMARY_WAVE_B_MANIFEST,
) -> uuid.UUID:
    readiness = wave_b_readiness(settings, preflight_id, manifest_path)
    if readiness["ready"] is not True:
        raise ValueError(f"Wave B execution is blocked by readiness gates: {readiness['blockers']}")
    _, wave_payload, primary_payload = _load_contract(manifest_path)
    campaign_id = create_campaign(
        settings,
        "v2-primary-comparison-wave-b",
        "PRIMARY",
        {"maximize": "throughput_tps", "minimize": "p99_ms"},
        {"learned_constraints": [], "hard_safety_gates": primary_payload["hard_safety_gates"]},
        failure_limit=WAVE_B_EXPECTED_OBSERVATIONS,
        campaign_settings={
            "protocol_id": "thesis-protocol-v2",
            "stage": WAVE_B_STAGE,
            "wave": "B",
            "manifest_sha256": readiness["manifest_sha256"],
            "source_wave_a_manifest_sha256": wave_payload["source_evidence"][
                "wave_a_manifest_sha256"
            ],
            "schedule_sha256": readiness["schedule_sha256"],
            "candidate_design_sha256": readiness["candidate_design_sha256"],
            "preflight_id": str(preflight_id),
            "candidate_restore_baseline_id": str(FROZEN_BASELINE_ID),
            "benchmark_profile_id": FROZEN_PROFILE_ID,
            "manifest": wave_payload,
        },
        actor="v2-primary-wave-b",
    )
    primary_block_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{campaign_id}:v2-primary-wave-b")
    default_configuration = {
        str(name): str(value)
        for name, value in dict(primary_payload["postgresql_default_configuration"]).items()
    }
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_primary_blocks
               (primary_block_id,campaign_id,protocol_id,evidence_role,manifest_sha256,
                preflight_id,baseline_id,benchmark_profile_id,schedule_sha256,
                candidate_design_sha256,status,retry_policy,drift_interpretation,wave)
               VALUES (%s,%s,'thesis-protocol-v2','PRIMARY',%s,%s,%s,%s,%s,%s,
                       'PLANNED',%s,%s,'B')""",
            (
                primary_block_id,
                campaign_id,
                readiness["manifest_sha256"],
                preflight_id,
                FROZEN_BASELINE_ID,
                FROZEN_PROFILE_ID,
                readiness["schedule_sha256"],
                readiness["candidate_design_sha256"],
                Jsonb(primary_payload["infrastructure_retry_policy"]),
                Jsonb(primary_payload["drift_interpretation"]),
            ),
        )
        for item in build_wave_b_plan(wave_payload, primary_payload):
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


def _wave_b_block(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    block = _primary_block(settings, campaign_id)
    if str(block.get("wave", "A")) != "B":
        raise ValueError(f"campaign {campaign_id} is not primary Wave B")
    return block


def run_wave_b_next(
    settings: Settings,
    campaign_id: uuid.UUID,
    *,
    owner: str = "v2-primary-wave-b",
    lease_seconds: int = 600,
) -> Any:
    manifest_sha, _, primary_payload = _load_contract(PRIMARY_WAVE_B_MANIFEST)
    block = _wave_b_block(settings, campaign_id)
    if str(block["manifest_sha256"]) != manifest_sha:
        raise ValueError("Wave B block manifest SHA-256 differs from the ready manifest")
    return _run_primary_next_with_contract(
        settings,
        campaign_id,
        payload=primary_payload,
        expected_observations=WAVE_B_EXPECTED_OBSERVATIONS,
        expected_wave="B",
        owner=owner,
        lease_seconds=lease_seconds,
    )


def wave_b_history(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    _wave_b_block(settings, campaign_id)
    return primary_history(settings, campaign_id)


def _wave_b_analysis_payload(primary_payload: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(primary_payload)
    payload["wave_a"] = {
        "seed_count": 2,
        "seeds": list(WAVE_B_SEEDS),
        "candidate_budget_per_method": 30,
        "physical_observations_per_seed": 131,
        "physical_observations": WAVE_B_EXPECTED_OBSERVATIONS,
    }
    payload["drift_interpretation"] = deepcopy(primary_payload["drift_interpretation"])
    payload["drift_interpretation"]["controls"] = 10
    return payload


def analyze_wave_b(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    block = _wave_b_block(settings, campaign_id)
    if block["status"] == "ANALYZED":
        return dict(block["analysis"])
    if block["status"] != "OBSERVATIONS_COMPLETE" or block["campaign_status"] != "PAUSED":
        raise ValueError("Wave B analysis requires a paused observations-complete block")
    _, wave_payload, primary_payload = _load_contract(PRIMARY_WAVE_B_MANIFEST)
    history = wave_b_history(settings, campaign_id)
    analysis_payload = _wave_b_analysis_payload(primary_payload)
    analysis = analyze_primary_observations(
        history["runs"],
        analysis_payload,
        seeds=list(WAVE_B_SEEDS),
        expected_observations=WAVE_B_EXPECTED_OBSERVATIONS,
        report_scope="Wave B",
    )
    analysis.update(
        {
            "wave_b_manifest_sha256": _file_sha256(PRIMARY_WAVE_B_MANIFEST),
            "source_wave_a_analysis_sha256": wave_payload["source_evidence"][
                "wave_a_analysis_payload_sha256"
            ],
            "standalone_before_combined": True,
            "endpoint_independence_guard": (
                "The five endpoints are correlated views of TPS and p99 and are not five "
                "independent confirmations."
            ),
            "method_family_guard": (
                "Report qLogNParEGO and qLogNEHVI individually but do not rank them; "
                "interpret the multi-objective methods as a family."
            ),
        }
    )
    analysis_sha = _canonical_sha256(analysis)
    primary_block_id = uuid.UUID(str(block["primary_block_id"]))
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_primary_blocks
               SET status='ANALYZED',analysis=%s,analysis_sha256=%s,
                   completed_at=clock_timestamp()
               WHERE primary_block_id=%s AND status='OBSERVATIONS_COMPLETE'""",
            (Jsonb(analysis), analysis_sha, primary_block_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Wave B block lost its observations-complete state")
        conn.commit()
    control_campaign(
        settings,
        campaign_id,
        "stop",
        f"Wave B standalone analysis complete with outcome {analysis['outcome']}",
        actor="v2-primary-wave-b",
    )
    return {**analysis, "analysis_sha256": analysis_sha}


def export_wave_b_analysis(
    settings: Settings,
    campaign_id: uuid.UUID,
    output_path: Path | None = None,
) -> dict[str, Any]:
    block = _wave_b_block(settings, campaign_id)
    if block["status"] != "ANALYZED" or block["analysis"] is None:
        raise ValueError("Wave B export requires a terminal standalone analysis")
    root = (settings.artifact_dir / WAVE_B_STAGE).resolve()
    output = (output_path or root / "primary-wave-b-analysis.json").resolve()
    if output != root and root not in output.parents:
        raise ValueError("Wave B analysis export must stay under its artifact root")
    payload = {
        "campaign_id": str(campaign_id),
        "primary_block_id": str(block["primary_block_id"]),
        "wave": "B",
        "analysis_sha256": block["analysis_sha256"],
        "analysis_sha256_semantics": "canonical JSON payload hash, not file hash",
        "analysis": block["analysis"],
        "history": wave_b_history(settings, campaign_id),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return {
        "output_path": str(output),
        "file_sha256": _file_sha256(output),
        "analysis_sha256": block["analysis_sha256"],
    }


def export_wave_b_report(
    settings: Settings,
    campaign_id: uuid.UUID,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    block = _wave_b_block(settings, campaign_id)
    if block["status"] != "ANALYZED" or block["analysis"] is None:
        raise ValueError("Wave B report requires a terminal standalone analysis")
    manifest_sha, wave_payload, primary_payload = _load_contract(PRIMARY_WAVE_B_MANIFEST)
    root = (settings.artifact_dir / WAVE_B_STAGE).resolve()
    output = (output_dir or root / "report").resolve()
    if output != root and root not in output.parents:
        raise ValueError("Wave B report must stay under its artifact root")
    context = primary_report_context(block, PRIMARY_MANIFEST)
    context.update(
        {
            "manifest_sha256": manifest_sha,
            "schedule_sha256": wave_payload["execution_schedule"]["schedule_sha256"],
            "candidate_design_sha256": wave_payload["execution_schedule"][
                "candidate_design_sha256"
            ],
            "wave": "B",
            "seeds": list(WAVE_B_SEEDS),
            "benchmark_profile_id": primary_payload["benchmark_profile_id"],
        }
    )
    return render_primary_report(dict(block["analysis"]), context, output)


def _summary_rows(
    seed_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    statistics_rows: list[dict[str, Any]] = []
    pairwise_rows: list[dict[str, Any]] = []
    for metric, direction in METRIC_DEFINITIONS.items():
        values_by_method: dict[str, list[float]] = {}
        for method in PRIMARY_SEARCH_METHODS:
            values = [
                float(row[metric])
                for row in seed_rows
                if row["method"] == method and row[metric] is not None
            ]
            if len(values) != 5:
                raise ValueError(f"combined analysis requires five {metric} values for {method}")
            values_by_method[method] = values
            summary = descriptive_summary(
                values,
                seed=_derived_seed(f"thesis-protocol-v2/primary/final/{metric}/{method}"),
            )
            statistics_rows.append(
                {
                    "metric": metric,
                    "direction": direction,
                    "method": method,
                    **summary.__dict__,
                    "bootstrap_unit": "seed",
                }
            )
        metric_pairs: list[dict[str, Any]] = []
        for left_index, left in enumerate(PRIMARY_SEARCH_METHODS):
            for right in PRIMARY_SEARCH_METHODS[left_index + 1 :]:
                comparison = paired_comparison(values_by_method[left], values_by_method[right])
                metric_pairs.append(
                    {
                        "metric": metric,
                        "direction": direction,
                        "baseline_method": left,
                        "treatment_method": right,
                        **comparison.__dict__,
                        "pairing_unit": "seed",
                    }
                )
        adjusted = holm_adjust([float(row["permutation_p_value"]) for row in metric_pairs])
        for row, value in zip(metric_pairs, adjusted, strict=True):
            row["holm_adjusted_p_value_within_metric_family"] = value
        pairwise_rows.extend(metric_pairs)
    return statistics_rows, pairwise_rows


def combine_primary_analyses(
    wave_a: dict[str, Any], wave_b: dict[str, Any], wave_payload: dict[str, Any]
) -> dict[str, Any]:
    if (
        wave_b.get("report_scope") != "Wave B"
        or wave_b.get("standalone_before_combined") is not True
    ):
        raise ValueError("combined analysis requires the registered standalone Wave B analysis")
    seed_rows = [
        *[dict(row) for row in wave_a["seed_method_results"]],
        *[dict(row) for row in wave_b["seed_method_results"]],
    ]
    observed_seeds = {int(row["seed"]) for row in seed_rows}
    expected_seeds = {
        88408573,
        1418705027,
        642754166,
        *WAVE_B_SEEDS,
    }
    if observed_seeds != expected_seeds or len(seed_rows) != 25:
        raise ValueError("combined analysis requires exactly five seeds by five methods")
    statistics_rows, pairwise_rows = _summary_rows(seed_rows)
    method_results: list[dict[str, Any]] = []
    reliability: list[dict[str, Any]] = []
    for method in PRIMARY_SEARCH_METHODS:
        rows = [row for row in seed_rows if row["method"] == method]
        method_results.append(
            {
                "method": method,
                "seed_count": 5,
                "valid_observations": sum(int(row["valid_observations"]) for row in rows),
                "mean_final_hypervolume": statistics.fmean(
                    float(row["hypervolume_at_0_negative_40"]) for row in rows
                ),
                "mean_seed_best_throughput_tps": statistics.fmean(
                    float(row["best_throughput_tps"]) for row in rows
                ),
                "mean_seed_minimum_p99_ms": statistics.fmean(
                    float(row["minimum_p99_ms"]) for row in rows
                ),
                "mean_control_relative_tps": statistics.fmean(
                    float(row["mean_control_relative_tps"]) for row in rows
                ),
                "mean_control_relative_p99_ms": statistics.fmean(
                    float(row["mean_control_relative_p99_ms"]) for row in rows
                ),
            }
        )
        valid = sum(int(row["valid_observations"]) for row in rows)
        dominating = sum(int(row["observations_dominating_local_control"]) for row in rows)
        reliability.append(
            {
                "method": method,
                "valid_candidate_observations": valid,
                "observations_dominating_local_control": dominating,
                "local_control_domination_share": dominating / valid if valid else None,
                "mean_control_relative_tps": method_results[-1]["mean_control_relative_tps"],
                "mean_control_relative_p99_ms": method_results[-1]["mean_control_relative_p99_ms"],
            }
        )
    return {
        "outcome": "COMPLETE",
        "report_scope": "combined Wave A + Wave B",
        "independent_unit": "seed",
        "independent_seed_count": 5,
        "analysis_seeds": sorted(expected_seeds),
        "registered_endpoints": list(METRIC_DEFINITIONS),
        "source_wave_a_analysis_sha256": wave_payload["source_evidence"][
            "wave_a_analysis_payload_sha256"
        ],
        "source_wave_b_analysis_sha256": wave_b.get("analysis_sha256"),
        "seed_method_results": seed_rows,
        "method_results": method_results,
        "seed_level_statistics": statistics_rows,
        "pairwise_comparisons": pairwise_rows,
        "candidate_reliability": reliability,
        "inference_guard": (
            "Seed is the independent unit (n=5). The exact two-sided sign-flip p-value "
            "cannot be below 0.0625; p-values are supplementary to per-seed direction "
            "and practical effect size."
        ),
        "endpoint_independence_guard": (
            "The five endpoints are correlated views of TPS and p99; endpoint wins are "
            "not independent confirmations."
        ),
        "interpretation_guard": (
            "Lead with reliable budget allocation and degradation avoidance, report p99 "
            "in milliseconds with modest scale context, and do not rank qLogNParEGO "
            "against qLogNEHVI."
        ),
        "scope_guard": (
            "Findings are limited to the recorded pgbench TPC-B-like OLTP scale-500/c32 "
            "environment and do not generalize to analytical or read-heavy workloads."
        ),
        "apply_best_authorized": False,
    }


def analyze_final_five_seed(
    settings: Settings,
    wave_b_campaign_id: uuid.UUID,
) -> dict[str, Any]:
    block = _wave_b_block(settings, wave_b_campaign_id)
    if block["status"] != "ANALYZED" or block["campaign_status"] != "STOPPED":
        raise ValueError("final five-seed analysis requires terminal standalone Wave B")
    report_index = settings.artifact_dir / WAVE_B_STAGE / "report" / "primary-report-index.json"
    if not report_index.is_file():
        raise ValueError("standalone Wave B report must be rendered before combined analysis")
    report_payload = json.loads(report_index.read_text(encoding="utf-8"))
    if report_payload.get("context", {}).get("analysis_sha256") != block["analysis_sha256"]:
        raise ValueError("standalone Wave B report does not authenticate the terminal analysis")
    _, wave_payload, _ = _load_contract(PRIMARY_WAVE_B_MANIFEST)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT b.analysis,b.analysis_sha256,b.status,c.status AS campaign_status
               FROM charm_control.experiment_v2_primary_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.campaign_id=%s""",
            (WAVE_A_CAMPAIGN_ID,),
        )
        wave_a_row = cur.fetchone()
    if (
        wave_a_row is None
        or wave_a_row["status"] != "ANALYZED"
        or wave_a_row["campaign_status"] != "STOPPED"
        or str(wave_a_row["analysis_sha256"])
        != wave_payload["source_evidence"]["wave_a_analysis_payload_sha256"]
    ):
        raise ValueError("final analysis cannot authenticate terminal Wave A")
    wave_b_analysis = dict(block["analysis"])
    wave_b_analysis["analysis_sha256"] = str(block["analysis_sha256"])
    result = combine_primary_analyses(dict(wave_a_row["analysis"]), wave_b_analysis, wave_payload)
    result["analysis_sha256"] = _canonical_sha256(result)
    return result


def export_final_five_seed_analysis(
    settings: Settings,
    wave_b_campaign_id: uuid.UUID,
    output_path: Path | None = None,
) -> dict[str, Any]:
    analysis = analyze_final_five_seed(settings, wave_b_campaign_id)
    root = (settings.artifact_dir / WAVE_B_STAGE).resolve()
    output = (output_path or root / "final-five-seed-analysis.json").resolve()
    if output != root and root not in output.parents:
        raise ValueError("final five-seed export must stay under the Wave B artifact root")
    payload = {
        "wave_b_campaign_id": str(wave_b_campaign_id),
        "analysis_sha256": analysis["analysis_sha256"],
        "analysis_sha256_semantics": "canonical JSON payload hash, not file hash",
        "analysis": analysis,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return {
        "output_path": str(output),
        "file_sha256": _file_sha256(output),
        "analysis_sha256": analysis["analysis_sha256"],
    }


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: json.dumps(value, sort_keys=True, separators=(",", ":"))
                if isinstance(value, (dict, list, tuple))
                else value
                for key, value in row.items()
            }
        )
    return buffer.getvalue().encode("utf-8")


def _markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join(["---"] * len(columns)) + "|"]
    for row in rows:
        values: list[str] = []
        for column in columns:
            value = row.get(column, "")
            values.append(f"{value:.6g}" if isinstance(value, float) else str(value))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def export_final_five_seed_report(
    settings: Settings,
    wave_b_campaign_id: uuid.UUID,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    analysis = analyze_final_five_seed(settings, wave_b_campaign_id)
    root = (settings.artifact_dir / WAVE_B_STAGE).resolve()
    output = (output_dir or root / "report-final-five-seed").resolve()
    if output != root and root not in output.parents:
        raise ValueError("final report must stay under the Wave B artifact root")
    output.mkdir(parents=True, exist_ok=True)
    tables = {
        "method-outcomes": analysis["method_results"],
        "seed-method-outcomes": analysis["seed_method_results"],
        "seed-level-statistics": analysis["seed_level_statistics"],
        "pairwise-contrasts": analysis["pairwise_comparisons"],
        "candidate-reliability": analysis["candidate_reliability"],
    }
    files: list[dict[str, Any]] = []
    method_rows = [
        {**row, "label": METHOD_LABELS[str(row["method"])]}
        for row in cast(list[dict[str, Any]], analysis["method_results"])
    ]
    reliability_rows = cast(list[dict[str, Any]], analysis["candidate_reliability"])
    markdown = "\n".join(
        [
            "# Protocol-v2 final five-seed primary analysis",
            "",
            f"- Wave B campaign: `{wave_b_campaign_id}`",
            f"- Analysis payload SHA-256: `{analysis['analysis_sha256']}`",
            "- Independent unit: seed (n=5)",
            "- Reporting order: Wave A standalone, Wave B standalone, then this combined report",
            "",
            "## Method outcomes",
            "",
            _markdown_table(
                method_rows,
                [
                    "label",
                    "mean_final_hypervolume",
                    "mean_seed_best_throughput_tps",
                    "mean_seed_minimum_p99_ms",
                    "mean_control_relative_tps",
                    "mean_control_relative_p99_ms",
                ],
            ),
            "",
            "## Candidate reliability",
            "",
            _markdown_table(
                reliability_rows,
                [
                    "method",
                    "valid_candidate_observations",
                    "observations_dominating_local_control",
                    "local_control_domination_share",
                    "mean_control_relative_tps",
                    "mean_control_relative_p99_ms",
                ],
            ),
            "",
            "## Frozen interpretation guards",
            "",
            f"- {analysis['inference_guard']}",
            f"- {analysis['endpoint_independence_guard']}",
            f"- {analysis['interpretation_guard']}",
            f"- {analysis['scope_guard']}",
            "- Apply-best is not authorized by this analysis.",
            "",
        ]
    ).encode("utf-8")
    report_path = output / "final-five-seed-report.md"
    report_path.write_bytes(markdown)
    files.append(
        {
            "path": report_path.relative_to(output).as_posix(),
            "bytes": len(markdown),
            "sha256": hashlib.sha256(markdown).hexdigest(),
        }
    )
    for name, rows in tables.items():
        data = _csv_bytes(cast(list[dict[str, Any]], rows))
        path = output / "tables" / f"{name}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files.append(
            {
                "path": path.relative_to(output).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    index = {
        "report_kind": "thesis-protocol-v2-primary-final-five-seed",
        "evidence_role": "PRIMARY",
        "deterministic": True,
        "analysis_sha256": analysis["analysis_sha256"],
        "table_row_counts": {name: len(rows) for name, rows in tables.items()},
        "files": files,
        "payload_sha256": _canonical_sha256(tables),
    }
    encoded = (json.dumps(index, indent=2, sort_keys=True) + "\n").encode("utf-8")
    index_path = output / "final-five-seed-report-index.json"
    index_path.write_bytes(encoded)
    return {
        "output_dir": str(output),
        "index_path": str(index_path),
        "index_file_sha256": hashlib.sha256(encoded).hexdigest(),
        "payload_sha256": index["payload_sha256"],
        "file_count": len(files) + 1,
        "total_bytes": sum(int(item["bytes"]) for item in files) + len(encoded),
    }


def wave_b_step_dict(step: Any) -> dict[str, Any]:
    return primary_step_dict(step)
