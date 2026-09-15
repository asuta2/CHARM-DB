from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scipy.stats import spearmanr  # type: ignore[import-untyped]

from charmdb.analysis.windows import analyze_prefix_windows
from charmdb.campaigns.primary import _interpolated_control
from charmdb.config import Settings
from charmdb.db import connect
from charmdb.protocol import load_manifest

MULTIFIDELITY_MANIFEST = Path("experiments/thesis/manifests/multi-fidelity.json")
PRIMARY_CAMPAIGN_ID = uuid.UUID("b0619879-c807-4ee3-859e-2c2dbec9934e")
PHYSICAL_OBSERVATIONS = 393
PHYSICAL_CANDIDATES = 378
PHYSICAL_CONTROLS = 15
F3_SECONDS = 600


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_path(root: Path, relative: object) -> Path:
    value = Path(str(relative))
    if value.is_absolute():
        raise ValueError("durable artifact path must be relative to the configured root")
    resolved_root = root.resolve()
    resolved = (resolved_root / value).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("durable artifact path escapes the configured root")
    return resolved


def _load_authenticated_observations(
    settings: Settings, campaign_id: uuid.UUID, windows: tuple[int, ...]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT b.*,c.status AS campaign_status
               FROM charm_control.experiment_v2_primary_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.campaign_id=%s""",
            (campaign_id,),
        )
        block_row = cur.fetchone()
        if block_row is None:
            raise ValueError(f"unknown primary campaign {campaign_id}")
        block = dict(block_row)
        if block["status"] != "ANALYZED" or block["campaign_status"] != "STOPPED":
            raise ValueError("Phase A requires the stopped, analyzed Wave A campaign")
        if _canonical_sha256(dict(block["analysis"])) != str(block["analysis_sha256"]):
            raise ValueError("primary analysis payload does not match its durable SHA-256")
        cur.execute(
            """SELECT r.primary_run_id,r.global_position,r.seed,r.within_seed_position,
                      r.evaluation_role,r.method,r.status AS run_status,
                      a.trial_id,a.status AS attempt_status,t.state AS trial_state,
                      t.objective_values,t.constraint_values,t.workflow_result,
                      EXTRACT(EPOCH FROM (t.completed_at-t.started_at)) AS total_seconds,
                      measurement.result AS measurement_result,
                      validation.result AS validation_result,
                      artifact.relative_path AS trial_artifact_relative_path,
                      artifact.sha256 AS trial_artifact_sha256,
                      artifact.byte_size AS trial_artifact_byte_size,
                      restore.exact_core_passed,restore.physical_statistics_passed
               FROM charm_control.experiment_v2_primary_runs r
               JOIN charm_control.experiment_v2_primary_attempts a
                 ON a.primary_run_id=r.primary_run_id AND a.status='COMPLETED'
               JOIN charm_control.trials t ON t.trial_id=a.trial_id
               JOIN charm_control.trial_action_executions measurement
                 ON measurement.trial_id=t.trial_id
                AND measurement.state='RUNNING_FULL_EVALUATION'
                AND measurement.status='COMPLETED'
               JOIN charm_control.trial_action_executions validation
                 ON validation.trial_id=t.trial_id
                AND validation.state='VALIDATING_MEASUREMENT'
                AND validation.status='COMPLETED'
               JOIN charm_control.artifacts artifact
                 ON artifact.trial_id=t.trial_id AND artifact.kind='durable-trial-json'
               JOIN charm_control.experiment_candidate_dataset_restores restore
                 ON restore.restore_id=t.candidate_dataset_restore_id
               WHERE r.primary_block_id=%s
               ORDER BY r.global_position""",
            (block["primary_block_id"],),
        )
        durable_rows = [dict(row) for row in cur.fetchall()]
    if len(durable_rows) != PHYSICAL_OBSERVATIONS:
        raise ValueError(
            f"Phase A requires {PHYSICAL_OBSERVATIONS} authenticated physical observations"
        )

    root = settings.artifact_dir.resolve()
    observations: list[dict[str, Any]] = []
    for row in durable_rows:
        trial_artifact = _artifact_path(root, row["trial_artifact_relative_path"])
        if not trial_artifact.is_file():
            raise ValueError(f"missing durable trial artifact {trial_artifact}")
        if trial_artifact.stat().st_size != int(row["trial_artifact_byte_size"]):
            raise ValueError(f"durable trial artifact byte-size mismatch {trial_artifact}")
        if _sha256(trial_artifact) != str(row["trial_artifact_sha256"]):
            raise ValueError(f"durable trial artifact SHA-256 mismatch {trial_artifact}")
        trial_payload = json.loads(trial_artifact.read_text(encoding="utf-8"))
        workflow_result = dict(row["workflow_result"] or {})
        measurement = dict(row["measurement_result"] or {})
        marker_relative = measurement.get("marker_relative_path")
        if not marker_relative or workflow_result.get("measurement_marker") != marker_relative:
            raise ValueError("measurement marker lineage differs between durable records")
        if trial_payload.get("measurement_marker") != marker_relative:
            raise ValueError("trial artifact measurement marker lineage mismatch")
        marker = _artifact_path(root, marker_relative)
        if not marker.is_file() or _sha256(marker) != str(measurement.get("marker_sha256")):
            raise ValueError(f"measurement marker authentication failed {marker}")
        prefix_error: str | None = None
        prefixes: dict[str, dict[str, Any]] = {}
        try:
            prefixes = {
                str(window): asdict(metrics)
                for window, metrics in analyze_prefix_windows(marker, windows).items()
            }
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            prefix_error = f"{type(error).__name__}: {error}"
        result = dict(measurement.get("result") or {})
        objectives = dict(row["objective_values"] or {})
        validation = dict(row["validation_result"] or {})
        hard_gates_passed = all(
            (
                row["run_status"] == "COMPLETED",
                row["attempt_status"] == "COMPLETED",
                row["trial_state"] == "COMPLETED",
                bool(row["exact_core_passed"]),
                bool(row["physical_statistics_passed"]),
                validation.get("valid") is True,
                int(result.get("failures", -1)) == 0,
            )
        )
        observations.append(
            {
                "primary_run_id": str(row["primary_run_id"]),
                "trial_id": str(row["trial_id"]),
                "global_position": int(row["global_position"]),
                "seed": int(row["seed"]),
                "within_seed_position": int(row["within_seed_position"]),
                "evaluation_role": str(row["evaluation_role"]),
                "method": str(row["method"]),
                "full_throughput_tps": float(objectives["throughput_tps"]),
                "full_p99_ms": float(objectives["p99_ms"]),
                "full_failures": int(result["failures"]),
                "total_seconds": float(row["total_seconds"]),
                "hard_gates_passed": hard_gates_passed,
                "authenticated": True,
                "prefix_error": prefix_error,
                "prefixes": prefixes,
                "marker_relative_path": str(marker_relative),
                "marker_sha256": str(measurement["marker_sha256"]),
                "trial_artifact_relative_path": str(row["trial_artifact_relative_path"]),
                "trial_artifact_sha256": str(row["trial_artifact_sha256"]),
            }
        )
    return observations, {
        "campaign_id": str(campaign_id),
        "primary_block_id": str(block["primary_block_id"]),
        "primary_analysis_sha256": str(block["analysis_sha256"]),
        "primary_manifest_sha256": str(block["manifest_sha256"]),
    }


def _rank_correlation(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    value = float(spearmanr(x, y).statistic)
    return value if math.isfinite(value) else None


def _controls(
    observations: list[dict[str, Any]], seed: int, window: int | None
) -> list[dict[str, float]]:
    controls: list[dict[str, float]] = []
    for row in observations:
        if row["seed"] != seed or row["method"] != "postgresql_default":
            continue
        if window is None:
            tps = row["full_throughput_tps"]
        else:
            prefix = row["prefixes"].get(str(window))
            if not row["hard_gates_passed"] or prefix is None or int(prefix["failures"]) != 0:
                continue
            tps = prefix["throughput_tps"]
        controls.append(
            {
                "position": float(row["within_seed_position"]),
                "throughput_tps": float(tps),
            }
        )
    controls.sort(key=lambda item: item["position"])
    if len(controls) != 5:
        raise ValueError(f"seed {seed} requires five valid default controls")
    return controls


def _candidate_rows(
    observations: list[dict[str, Any]], windows: tuple[int, ...]
) -> list[dict[str, Any]]:
    candidates = [row for row in observations if row["method"] != "postgresql_default"]
    if len(candidates) != PHYSICAL_CANDIDATES:
        raise ValueError(f"Phase A requires {PHYSICAL_CANDIDATES} unique physical candidates")
    seeds = sorted({int(row["seed"]) for row in candidates})
    control_index = {
        (seed, window): _controls(observations, seed, window)
        for seed in seeds
        for window in (None, *windows)
    }
    result: list[dict[str, Any]] = []
    for row in candidates:
        seed = int(row["seed"])
        position = int(row["within_seed_position"])
        full_control = _interpolated_control(
            control_index[(seed, None)], position, "throughput_tps"
        )
        item = {
            **row,
            "full_control_tps": full_control,
            "full_control_relative_tps": float(row["full_throughput_tps"]) / full_control,
            "window_metrics": {},
        }
        for window in windows:
            prefix = row["prefixes"].get(str(window))
            prefix_valid = (
                row["authenticated"]
                and row["hard_gates_passed"]
                and prefix is not None
                and int(prefix["failures"]) == 0
            )
            control_tps = _interpolated_control(
                control_index[(seed, window)], position, "throughput_tps"
            )
            item["window_metrics"][str(window)] = {
                "valid": prefix_valid,
                "throughput_tps": float(prefix["throughput_tps"]) if prefix else None,
                "p99_ms": float(prefix["p99_ms"]) if prefix else None,
                "failures": int(prefix["failures"]) if prefix else None,
                "control_tps": control_tps,
                "control_relative_tps": (
                    float(prefix["throughput_tps"]) / control_tps if prefix_valid else None
                ),
            }
        result.append(item)
    for seed in seeds:
        members = sorted(
            (row for row in result if row["seed"] == seed),
            key=lambda row: (-float(row["full_control_relative_tps"]), row["global_position"]),
        )
        top_count = math.ceil(len(members) * 0.25)
        top_ids = {row["primary_run_id"] for row in members[:top_count]}
        for row in members:
            row["full_fidelity_top_quartile"] = row["primary_run_id"] in top_ids
    return result


def analyze_phase_a_observations(
    observations: list[dict[str, Any]], manifest: dict[str, Any]
) -> dict[str, Any]:
    phase = dict(manifest["phase_a"])
    windows = tuple(int(value) for value in phase["window_seconds"])
    floor = float(phase["promotion_rule"]["throughput_floor_ratio"])
    sensitivity = [float(value) for value in phase["threshold_sensitivity_ratios"]]
    if len(observations) != PHYSICAL_OBSERVATIONS:
        raise ValueError(f"Phase A requires exactly {PHYSICAL_OBSERVATIONS} observations")
    if len({row["primary_run_id"] for row in observations}) != len(observations):
        raise ValueError("Phase A physical observations must be unique")
    candidates = _candidate_rows(observations, windows)
    seeds = sorted({int(row["seed"]) for row in candidates})
    summaries: list[dict[str, Any]] = []
    sensitivities: list[dict[str, Any]] = []
    decisions: dict[str, dict[str, bool]] = {}
    control_total = sum(
        float(row["total_seconds"])
        for row in observations
        if row["method"] == "postgresql_default"
    )
    baseline_candidate_total = sum(
        max(0.0, float(row["total_seconds"]) - F3_SECONDS) + F3_SECONDS
        for row in candidates
    )
    f3_only_seconds = control_total + baseline_candidate_total
    for window in windows:
        window_key = str(window)
        for row in candidates:
            metrics = row["window_metrics"][window_key]
            row[f"promoted_{window}"] = bool(
                metrics["valid"] and float(metrics["control_relative_tps"]) >= floor
            )
        window_seed_passes: dict[str, bool] = {}
        for seed in seeds:
            members = [row for row in candidates if row["seed"] == seed]
            eligible = [
                row
                for row in members
                if row["window_metrics"][window_key]["control_relative_tps"] is not None
            ]
            rho = _rank_correlation(
                [
                    float(row["window_metrics"][window_key]["control_relative_tps"])
                    for row in eligible
                ],
                [float(row["full_control_relative_tps"]) for row in eligible],
            )
            false_rejections = sum(
                row["full_fidelity_top_quartile"] and not row[f"promoted_{window}"]
                for row in members
            )
            promoted = sum(row[f"promoted_{window}"] for row in members)
            passed = (
                rho is not None
                and rho >= float(phase["minimum_spearman"])
                and false_rejections <= int(phase["maximum_top_quartile_false_rejections"])
            )
            window_seed_passes[str(seed)] = passed
            summaries.append(
                {
                    "window_seconds": window,
                    "seed": seed,
                    "candidates": len(members),
                    "valid_prefixes": len(eligible),
                    "spearman_control_relative_tps": rho,
                    "promoted": promoted,
                    "rejected": len(members) - promoted,
                    "top_quartile": sum(row["full_fidelity_top_quartile"] for row in members),
                    "top_quartile_false_rejections": false_rejections,
                    "rank_and_false_rejection_gate_passed": passed,
                }
            )
        eligible_all = [
            row
            for row in candidates
            if row["window_metrics"][window_key]["control_relative_tps"] is not None
        ]
        promoted_all = sum(row[f"promoted_{window}"] for row in candidates)
        phase_seconds = control_total + sum(
            max(0.0, float(row["total_seconds"]) - F3_SECONDS)
            + (F3_SECONDS if row[f"promoted_{window}"] else window)
            for row in candidates
        )
        time_passed = phase_seconds < f3_only_seconds
        summaries.append(
            {
                "window_seconds": window,
                "seed": "pooled-descriptive",
                "candidates": len(candidates),
                "valid_prefixes": len(eligible_all),
                "spearman_control_relative_tps": _rank_correlation(
                    [
                        float(row["window_metrics"][window_key]["control_relative_tps"])
                        for row in eligible_all
                    ],
                    [float(row["full_control_relative_tps"]) for row in eligible_all],
                ),
                "promoted": promoted_all,
                "rejected": len(candidates) - promoted_all,
                "top_quartile": sum(row["full_fidelity_top_quartile"] for row in candidates),
                "top_quartile_false_rejections": sum(
                    row["full_fidelity_top_quartile"] and not row[f"promoted_{window}"]
                    for row in candidates
                ),
                "modeled_f3_only_seconds": f3_only_seconds,
                "modeled_phase_a_seconds": phase_seconds,
                "modeled_seconds_saved": f3_only_seconds - phase_seconds,
                "modeled_fraction_saved": (f3_only_seconds - phase_seconds) / f3_only_seconds,
                "time_gate_passed": time_passed,
                "pooled_is_acceptance_evidence": False,
            }
        )
        decisions[window_key] = {
            "every_seed_rank_and_false_rejection_gate_passed": all(
                window_seed_passes.values()
            ),
            "time_gate_passed": time_passed,
        }
        for ratio in sensitivity:
            promoted = sum(
                row["window_metrics"][window_key]["valid"]
                and float(row["window_metrics"][window_key]["control_relative_tps"]) >= ratio
                for row in candidates
            )
            false_rejections = sum(
                row["full_fidelity_top_quartile"]
                and not (
                    row["window_metrics"][window_key]["valid"]
                    and float(row["window_metrics"][window_key]["control_relative_tps"])
                    >= ratio
                )
                for row in candidates
            )
            sensitivities.append(
                {
                    "window_seconds": window,
                    "throughput_floor_ratio": ratio,
                    "promoted": promoted,
                    "rejected": len(candidates) - promoted,
                    "top_quartile_false_rejections": false_rejections,
                    "measurement_seconds_saved": (len(candidates) - promoted)
                    * (F3_SECONDS - window),
                }
            )
    adopted = all(all(item.values()) for item in decisions.values())
    return {
        "evidence_role": "SECONDARY",
        "analysis_kind": "retrospective-multi-fidelity-phase-a",
        "new_benchmark_observations": 0,
        "physical_observations": len(observations),
        "physical_candidate_observations": len(candidates),
        "physical_default_controls": len(observations) - len(candidates),
        "shared_initialization_counted_once": True,
        "promotion_floor_ratio": floor,
        "p99_is_promotion_constraint": False,
        "summaries": summaries,
        "threshold_sensitivity": sensitivities,
        "window_gate_results": decisions,
        "decision": (
            "ELIGIBLE_FOR_OPTIONAL_PHASE_B" if adopted else "REJECT_ADAPTIVE_MULTI_FIDELITY"
        ),
        "adopted": adopted,
        "observations": candidates,
    }


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    columns: list[str] = []
    for row in rows:
        columns.extend(key for key in row if key not in columns)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
                for key, value in row.items()
            }
        )
    return buffer.getvalue().encode()


def _markdown(analysis: dict[str, Any], context: dict[str, Any]) -> str:
    pooled = [row for row in analysis["summaries"] if row["seed"] == "pooled-descriptive"]
    return "\n".join(
        [
            "# Multi-fidelity Phase A retrospective analysis",
            "",
            "**Evidence role: SECONDARY. No benchmark was executed.**",
            "",
            f"- Campaign: `{context['campaign_id']}`",
            f"- Primary analysis SHA-256: `{context['primary_analysis_sha256']}`",
            f"- Decision: **{analysis['decision']}**",
            "- Promotion: valid/hard-gate-passed prefix TPS >= 0.80 times the "
            "same-window same-seed interpolated default.",
            "- p99 is an objective, not a promotion constraint.",
            "- Shared BO initialization is counted once physically.",
            "",
            "| window | pooled descriptive Spearman | promoted | false rejections | "
            "modeled hours saved | fraction saved |",
            "|---:|---:|---:|---:|---:|---:|",
            *[
                f"| {row['window_seconds']} | {row['spearman_control_relative_tps']:.6f} | "
                f"{row['promoted']} | {row['top_quartile_false_rejections']} | "
                f"{row['modeled_seconds_saved'] / 3600:.3f} | "
                f"{row['modeled_fraction_saved']:.6f} |"
                for row in pooled
            ],
            "",
            "Adoption requires every seed at both windows to pass the rank and "
            "zero-false-rejection gates, plus lower modeled total time. Pooled "
            "correlation is descriptive only.",
            "",
        ]
    )


def _write(path: Path, content: bytes, root: Path, files: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    files.append(
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    )


def execute_phase_a(
    settings: Settings,
    campaign_id: uuid.UUID = PRIMARY_CAMPAIGN_ID,
    output_dir: Path | None = None,
    manifest_path: Path = MULTIFIDELITY_MANIFEST,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    if manifest.status != "ready" or manifest.payload.get("execution_ready") is not True:
        raise ValueError("Multi-fidelity Phase A manifest is not execution-ready")
    windows = tuple(int(value) for value in manifest.payload["phase_a"]["window_seconds"])
    observations, context = _load_authenticated_observations(settings, campaign_id, windows)
    analysis = analyze_phase_a_observations(observations, manifest.payload)
    analysis_sha256 = _canonical_sha256(analysis)
    root = (output_dir or settings.artifact_dir / "multi-fidelity" / "phase-a").resolve()
    artifact_root = settings.artifact_dir.resolve()
    if root != artifact_root and artifact_root not in root.parents:
        raise ValueError("Phase A output must stay under the configured artifact root")
    root.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    analysis_payload = {
        **context,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "analysis_sha256": analysis_sha256,
        "analysis_sha256_semantics": "canonical JSON hash of analysis payload",
        "analysis": analysis,
    }
    _write(
        root / "phase-a-analysis.json",
        (json.dumps(analysis_payload, indent=2, sort_keys=True) + "\n").encode(),
        root,
        files,
    )
    _write(root / "phase-a-report.md", _markdown(analysis, context).encode(), root, files)
    _write(root / "phase-a-summary.csv", _csv_bytes(analysis["summaries"]), root, files)
    _write(
        root / "phase-a-threshold-sensitivity.csv",
        _csv_bytes(analysis["threshold_sensitivity"]),
        root,
        files,
    )
    observation_rows: list[dict[str, Any]] = []
    for row in analysis["observations"]:
        item = {
            key: value
            for key, value in row.items()
            if key not in {"prefixes", "window_metrics"}
        }
        for window in windows:
            metrics = row["window_metrics"][str(window)]
            item.update({f"prefix_{window}_{key}": value for key, value in metrics.items()})
        observation_rows.append(item)
    _write(
        root / "phase-a-observations.csv", _csv_bytes(observation_rows), root, files
    )
    index = {
        "report_kind": "multi-fidelity-phase-a",
        "evidence_role": "SECONDARY",
        "new_benchmark_observations": 0,
        "decision": analysis["decision"],
        "analysis_sha256": analysis_sha256,
        "files": files,
    }
    encoded_index = (json.dumps(index, indent=2, sort_keys=True) + "\n").encode()
    index_path = root / "phase-a-report-index.json"
    index_path.write_bytes(encoded_index)
    return {
        "output_dir": str(root),
        "index_path": str(index_path),
        "index_file_sha256": hashlib.sha256(encoded_index).hexdigest(),
        "analysis_sha256": analysis_sha256,
        "decision": analysis["decision"],
        "file_count": len(files) + 1,
        "total_bytes": sum(int(item["bytes"]) for item in files) + len(encoded_index),
    }
