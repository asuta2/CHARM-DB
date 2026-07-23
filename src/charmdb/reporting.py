from __future__ import annotations

import csv
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from charmdb.config import Settings
from charmdb.db import connect


@dataclass(frozen=True)
class ReportResult:
    output_dir: Path
    markdown_path: Path
    json_path: Path
    csv_path: Path
    manifest_path: Path
    chart_path: Path | None
    campaigns: int
    trials: int


def _svg_best_so_far(rows: list[dict[str, Any]], path: Path) -> Path | None:
    points = [float(row["throughput_tps"]) for row in rows if row["throughput_tps"] is not None]
    if not points:
        return None
    best: list[float] = []
    current = -math.inf
    for value in points:
        current = max(current, value)
        best.append(current)
    width, height, margin = 900, 420, 55
    low, high = min(best), max(best)
    span = max(high - low, 1.0)
    coordinates = []
    for index, value in enumerate(best):
        x = margin + (width - 2 * margin) * index / max(1, len(best) - 1)
        y = height - margin - (height - 2 * margin) * (value - low) / span
        coordinates.append(f"{x:.2f},{y:.2f}")
    svg = "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
            '<rect width="100%" height="100%" fill="white"/>',
            f'<line x1="{margin}" y1="{height - margin}" x2="{width - margin}" '
            f'y2="{height - margin}" stroke="#222"/>',
            f'<line x1="{margin}" y1="{margin}" x2="{margin}" '
            f'y2="{height - margin}" stroke="#222"/>',
            '<polyline fill="none" stroke="#2563eb" stroke-width="3" '
            f'points="{" ".join(coordinates)}"/>',
            f'<text x="{width / 2}" y="28" text-anchor="middle" '
            'font-family="sans-serif" font-size="18">Best observed throughput over measured '
            "trials</text>",
            f'<text x="{width / 2}" y="{height - 12}" text-anchor="middle" '
            'font-family="sans-serif">Measured trial order</text>',
            f'<text x="18" y="{height / 2}" transform="rotate(-90 18 {height / 2})" '
            'text-anchor="middle" font-family="sans-serif">TPS</text>',
            f'<text x="{margin}" y="{margin - 8}" font-family="sans-serif" '
            f'font-size="12">{high:.3f}</text>',
            f'<text x="{margin}" y="{height - margin + 18}" font-family="sans-serif" '
            f'font-size="12">{low:.3f}</text>',
            "</svg>",
        ]
    )
    path.write_text(svg, encoding="utf-8")
    return path


def generate_report(settings: Settings, output_root: Path | None = None) -> ReportResult:
    generated_at = datetime.now(UTC)
    root = output_root or settings.artifact_dir / "reports"
    output_dir = root / generated_at.strftime("%Y%m%dT%H%M%S%fZ")
    output_dir.mkdir(parents=True, exist_ok=False)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS count FROM charm_control.campaigns")
        campaign_row = cur.fetchone()
        if campaign_row is None:
            raise RuntimeError("campaign count query returned no row")
        campaign_count = int(campaign_row["count"])
        trial_query = """SELECT t.trial_id,t.campaign_id,c.name AS campaign_name,c.mode,
                      t.workflow_kind,
                      t.state,t.benchmark_profile,t.fidelity,t.random_seed,t.attempt_count,
                      COALESCE((t.objective_values->>'throughput_tps')::double precision,
                               (t.workflow_result->>'throughput_tps')::double precision)
                          AS throughput_tps,
                      COALESCE((t.objective_values->>'p99_ms')::double precision,
                               (t.workflow_result->>'p99_ms')::double precision) AS p99_ms,
                      t.failure_type,t.created_at,t.completed_at
               FROM charm_control.trials t JOIN charm_control.campaigns c USING(campaign_id)
               ORDER BY t.created_at"""
        cur.execute(trial_query)
        rows = [dict(row) for row in cur.fetchall()]
        multi_run_query = """SELECT run_id,campaign_id,random_seed,reference_point,
                   objective_definition,constraint_definition,candidate_pool_size,sample_count,
                   software_versions,status,created_at,completed_at
            FROM charm_control.multi_objective_runs ORDER BY created_at"""
        cur.execute(multi_run_query)
        multi_runs = [dict(row) for row in cur.fetchall()]
        recommendation_query = """SELECT recommendation_id,run_id,observation_id,iteration,method,
                   input_vector,configuration,workload_context,fidelity,pool_seed,acquisition_value,
                   probability_feasible,training_observation_ids,created_at
            FROM charm_control.multi_objective_recommendations ORDER BY created_at"""
        cur.execute(recommendation_query)
        recommendations = [dict(row) for row in cur.fetchall()]
        pareto_query = """SELECT snapshot_id,run_id,iteration,reference_point,
                   feasible_observation_ids,pareto_observation_ids,hypervolume,created_at
            FROM charm_control.pareto_snapshots ORDER BY created_at"""
        cur.execute(pareto_query)
        pareto_snapshots = [dict(row) for row in cur.fetchall()]
        champion_query = """SELECT champion_id,run_id,selected_observation_id,rule,
                   f4_observation_ids,context_scores,robust_score,worst_p99_ms,status,details,created_at
            FROM charm_control.champion_selections ORDER BY created_at"""
        cur.execute(champion_query)
        champions = [dict(row) for row in cur.fetchall()]
        gate_query = """SELECT decision_id,recommendation_id,calibration_report_id,
                   fidelity_report_id,probabilistic_gate_enabled,early_stopping_enabled,
                   decision,reasons,created_at
            FROM charm_control.optimizer_gate_decisions ORDER BY created_at"""
        cur.execute(gate_query)
        gate_decisions = [dict(row) for row in cur.fetchall()]
        experiment_group_query = """SELECT group_id,group_key,group_kind,hypothesis,baseline,
                   budget_unit,budget_value,seeds,required_metrics,prerequisite_gate,status,
                   blocked_reason,preregistered_at,completed_at
            FROM charm_control.experiment_groups ORDER BY group_kind,group_key"""
        cur.execute(experiment_group_query)
        experiment_groups = [dict(row) for row in cur.fetchall()]
        experiment_arm_query = """SELECT arm_id,group_id,label,method_definition,random_seed,
                   block_order,budget_value,campaign_id,status,manifest_sha256,gate_snapshot,
                   realized_budget_value,budget_breakdown,started_at,failure_reason,
                   created_at,completed_at
            FROM charm_control.experiment_arms ORDER BY group_id,random_seed,block_order"""
        cur.execute(experiment_arm_query)
        experiment_arms = [dict(row) for row in cur.fetchall()]
        experiment_event_query = """SELECT event_id,arm_id,event_type,previous_status,new_status,
                   actor,reason,details,occurred_at
            FROM charm_control.experiment_arm_events ORDER BY event_id"""
        cur.execute(experiment_event_query)
        experiment_arm_events = [dict(row) for row in cur.fetchall()]
        experiment_budget_query = """SELECT budget_entry_id,arm_id,trial_id,phase,fidelity,
                   wall_clock_seconds,unit_value,accounting_kind,operational_cost,evidence,created_at
            FROM charm_control.experiment_budget_entries ORDER BY created_at,budget_entry_id"""
        cur.execute(experiment_budget_query)
        experiment_budget_entries = [dict(row) for row in cur.fetchall()]
        experiment_search_query = """SELECT recommendation_id,arm_id,budget_position,method,
                   stage,random_seed,input_vector,configuration,training_observation_ids,
                   acquisition_name,acquisition_value,probability_feasible,trial_id,created_at
            FROM charm_control.experiment_search_recommendations
            ORDER BY arm_id,budget_position"""
        cur.execute(experiment_search_query)
        experiment_search_recommendations = [dict(row) for row in cur.fetchall()]
        manifest_preflight_query = """SELECT preflight_id,dataset_snapshot_sha256,schema_sha256,
                   dataset_content_sha256,
                   snapshot_relative_path,snapshot_byte_size,evidence_relative_path,evidence_sha256,
                   evidence_byte_size,eligible,unresolved,evidence,created_at
            FROM charm_control.experiment_manifest_preflights ORDER BY created_at,preflight_id"""
        cur.execute(manifest_preflight_query)
        manifest_preflights = [dict(row) for row in cur.fetchall()]
        restore_validation_query = """SELECT validation_id,preflight_id,
                   expected_snapshot_sha256,expected_schema_sha256,expected_content_sha256,
                   observed_schema_sha256,observed_content_sha256,passed,details,created_at
            FROM charm_control.experiment_dataset_restore_validations
            ORDER BY created_at,validation_id"""
        cur.execute(restore_validation_query)
        restore_validations = [dict(row) for row in cur.fetchall()]
        f3_reference_query = """SELECT block_id,preflight_id,campaign_id,warmup_seconds,
                   measurement_seconds,concurrency,required_trials,trial_ids,completed_trials,
                   reference_wall_clock_seconds,passed,details,created_at,completed_at
            FROM charm_control.experiment_f3_reference_blocks ORDER BY created_at,block_id"""
        cur.execute(f3_reference_query)
        f3_reference_blocks = [dict(row) for row in cur.fetchall()]
        manifest_review_query = """SELECT review_id,preflight_id,arm_id,execution_sha256,
                   frozen_manifest_sha256,execution_relative_path,execution_byte_size,passed,
                   reasons,review,created_at
            FROM charm_control.experiment_manifest_reviews ORDER BY created_at,review_id"""
        cur.execute(manifest_review_query)
        manifest_reviews = [dict(row) for row in cur.fetchall()]
        arm_restore_query = """SELECT arm_id,preflight_id,validation_id,manifest_sha256,created_at
            FROM charm_control.experiment_arm_dataset_restores ORDER BY created_at,arm_id"""
        cur.execute(arm_restore_query)
        arm_dataset_restores = [dict(row) for row in cur.fetchall()]
        coordination_query = """SELECT coordination_id,campaign_id,strategy,budget_position,
                   knob_configuration,index_candidate_ids,component_scores,interaction_score,
                   selected_score,training_observation_ids,experiment_arm_id,cost_variant,
                   predicted_operational_cost,created_at
            FROM charm_control.coordination_decisions ORDER BY created_at"""
        cur.execute(coordination_query)
        coordination_decisions = [dict(row) for row in cur.fetchall()]
        analysis_query = """SELECT analysis_report_id,status,independent_unit,registered_groups,
                   registered_arms,completed_arms,missing_arms,methods,relative_path,sha256,
                   byte_size,created_at
            FROM charm_control.analysis_reports ORDER BY created_at"""
        cur.execute(analysis_query)
        analysis_reports = [dict(row) for row in cur.fetchall()]
    state_counts = Counter(str(row["state"]) for row in rows)
    measured = [row for row in rows if row["throughput_tps"] is not None]
    payload = {
        "generated_at": generated_at.isoformat(),
        "campaign_count": campaign_count,
        "trial_count": len(rows),
        "measured_trial_count": len(measured),
        "state_counts": dict(sorted(state_counts.items())),
        "trials": rows,
        "multi_objective_runs": multi_runs,
        "multi_objective_recommendations": recommendations,
        "pareto_snapshots": pareto_snapshots,
        "champion_selections": champions,
        "optimizer_gate_decisions": gate_decisions,
        "experiment_groups": experiment_groups,
        "experiment_arms": experiment_arms,
        "experiment_arm_events": experiment_arm_events,
        "experiment_budget_entries": experiment_budget_entries,
        "experiment_search_recommendations": experiment_search_recommendations,
        "experiment_manifest_preflights": manifest_preflights,
        "experiment_dataset_restore_validations": restore_validations,
        "experiment_f3_reference_blocks": f3_reference_blocks,
        "experiment_manifest_reviews": manifest_reviews,
        "experiment_arm_dataset_restores": arm_dataset_restores,
        "coordination_decisions": coordination_decisions,
        "analysis_reports": analysis_reports,
        "evidence_warning": (
            "This export summarizes stored observations and is not a statistical "
            "significance claim."
        ),
    }
    json_path = output_dir / "summary.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    csv_path = output_dir / "trials.csv"
    fieldnames = list(rows[0]) if rows else ["trial_id"]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    chart_path = _svg_best_so_far(measured, output_dir / "best-so-far-throughput.svg")
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": generated_at.isoformat(),
                "filters": {"campaigns": "all", "trials": "all", "ordering": "created_at"},
                "queries": {
                    "trials": trial_query,
                    "multi_objective_runs": multi_run_query,
                    "multi_objective_recommendations": recommendation_query,
                    "pareto_snapshots": pareto_query,
                    "champion_selections": champion_query,
                    "optimizer_gate_decisions": gate_query,
                    "experiment_groups": experiment_group_query,
                    "experiment_arms": experiment_arm_query,
                    "experiment_arm_events": experiment_event_query,
                    "experiment_budget_entries": experiment_budget_query,
                    "experiment_search_recommendations": experiment_search_query,
                    "experiment_manifest_preflights": manifest_preflight_query,
                    "experiment_dataset_restore_validations": restore_validation_query,
                    "experiment_f3_reference_blocks": f3_reference_query,
                    "experiment_manifest_reviews": manifest_review_query,
                    "experiment_arm_dataset_restores": arm_restore_query,
                    "coordination_decisions": coordination_query,
                    "analysis_reports": analysis_query,
                },
                "source_primary_keys": {
                    "trials": "trial_id",
                    "multi_objective_runs": "run_id",
                    "multi_objective_recommendations": "recommendation_id",
                    "pareto_snapshots": "snapshot_id",
                    "champion_selections": "champion_id",
                    "optimizer_gate_decisions": "decision_id",
                    "experiment_groups": "group_id",
                    "experiment_arms": "arm_id",
                    "experiment_arm_events": "event_id",
                    "experiment_budget_entries": "budget_entry_id",
                    "experiment_search_recommendations": "recommendation_id",
                    "experiment_manifest_preflights": "preflight_id",
                    "experiment_dataset_restore_validations": "validation_id",
                    "experiment_f3_reference_blocks": "block_id",
                    "experiment_manifest_reviews": "review_id",
                    "experiment_arm_dataset_restores": "arm_id",
                    "coordination_decisions": "coordination_id",
                    "analysis_reports": "analysis_report_id",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    markdown_path = output_dir / "report.md"
    states = "\n".join(f"- `{state}`: {count}" for state, count in sorted(state_counts.items()))
    measured_values = [float(row["throughput_tps"]) for row in measured]
    best = max(measured_values) if measured_values else None
    best_text = f"{best:.6f} TPS" if best is not None else "not available"
    markdown_path.write_text(
        f"""# CHARM-DB reproducible evidence summary

Generated at: `{generated_at.isoformat()}`

This report is generated from persisted control-database observations. It does not by itself
establish effectiveness, causality, novelty, or statistical significance.

## Inventory

- Campaigns: {campaign_count}
- Trials: {len(rows)}
- Trials with stored throughput: {len(measured)}
- Best stored throughput (uncontrolled cross-campaign maximum): {best_text}

## Trial states

{states or "- No trials"}

## Reproducible files

- `summary.json`: complete machine-readable export
- `trials.csv`: tabular trial export
- `manifest.json`: exact export queries, filters, ordering, and source primary keys
- `best-so-far-throughput.svg`: descriptive stored-trial chart when measurements exist

## Interpretation boundary

The best-so-far chart spans stored trials that may use different workloads, durations, seeds, and
contexts. Use campaign-filtered equal-budget analysis before drawing scientific conclusions.
""",
        encoding="utf-8",
    )
    return ReportResult(
        output_dir,
        markdown_path,
        json_path,
        csv_path,
        manifest_path,
        chart_path,
        campaign_count,
        len(rows),
    )
