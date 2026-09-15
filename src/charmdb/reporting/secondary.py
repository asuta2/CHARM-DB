# ruff: noqa: E501
"""Deterministic exploratory showcase reporting for terminal Wave A evidence.

The displays in this module implement the formulas registered by D050.  They
consume the immutable ``primary-analysis.json`` export, never query either
database, and never alter the five registered PRIMARY endpoints.  Candidate
rows are descriptive; seed is the only inferential unit.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import statistics
from collections.abc import Callable
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from charmdb.optimization.design import BO_METHODS, PRIMARY_PARAMETER_ORDER
from charmdb.protocol import PRIMARY_SEARCH_METHODS
from charmdb.reporting.primary import (
    DEFAULT_CONTROL_METHOD,
    METHOD_COLORS,
    METHOD_LABELS,
    SHARED_INITIAL_METHOD,
    valid_primary_row,
)

SECONDARY_REPORT_KIND = "thesis-protocol-v2-primary-wave-a-secondary-showcases"
SECONDARY_EVIDENCE_ROLE = "EXPLORATORY"

_QUADRANTS = ("better_both", "tps_only", "p99_only", "worse_both")
_QUADRANT_COLORS = {
    "better_both": "#2a9d8f",
    "tps_only": "#457b9d",
    "p99_only": "#e9c46a",
    "worse_both": "#e76f51",
}
_CONSISTENCY_METRICS: tuple[tuple[str, str, bool], ...] = (
    ("hypervolume_at_0_negative_40", "Final hypervolume", True),
    ("best_throughput_tps", "Best TPS", True),
    ("minimum_p99_ms", "Minimum p99 (ms)", False),
    ("mean_control_relative_tps", "Mean control-relative TPS", True),
    ("mean_control_relative_p99_ms", "Mean control-relative p99 (ms)", False),
    ("local_control_domination_rate", "Local-control domination rate", True),
)


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_terminal_primary_export(path: Path) -> tuple[dict[str, Any], str]:
    """Load and authenticate the terminal analysis export used by D050."""
    raw = path.read_bytes()
    if raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("primary analysis export must be UTF-8 without a byte-order mark")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("primary analysis export must be a JSON object")
    for key in ("campaign_id", "primary_block_id", "analysis_sha256", "analysis", "history"):
        if key not in payload:
            raise ValueError(f"primary analysis export is missing {key}")
    analysis = payload["analysis"]
    history = payload["history"]
    if not isinstance(analysis, dict) or not isinstance(history, dict):
        raise ValueError("primary analysis and history must be JSON objects")
    expected = str(payload["analysis_sha256"])
    actual = _canonical_sha256(analysis)
    if actual != expected:
        raise ValueError(
            f"primary analysis payload SHA-256 mismatch: expected {expected}, computed {actual}"
        )
    runs = history.get("runs")
    if not isinstance(runs, list) or len(runs) != 393:
        raise ValueError("secondary showcases require the complete 393-slot Wave A ledger")
    if analysis.get("outcome") not in {"COMPLETE", "COMPLETE_WITH_DRIFT_FLAGS"}:
        raise ValueError("secondary showcases require a terminal complete Wave A analysis")
    return payload, hashlib.sha256(raw).hexdigest()


def _seed_order(runs: list[dict[str, Any]]) -> list[int]:
    result: list[int] = []
    for row in sorted(runs, key=lambda item: int(item["global_position"])):
        seed = int(row["seed"])
        if seed not in result:
            result.append(seed)
    if len(result) != 3:
        raise ValueError("secondary showcases require exactly three Wave A seeds")
    return result


def _logical_rows(runs: list[dict[str, Any]], seed: int, method: str) -> list[dict[str, Any]]:
    rows = [
        row
        for row in runs
        if int(row["seed"]) == seed
        and (
            row["method"] == method
            or (method in BO_METHODS and row["method"] == SHARED_INITIAL_METHOD)
        )
    ]
    return sorted(rows, key=lambda item: int(item["global_position"]))


def _controls(runs: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    controls = [
        row
        for row in runs
        if int(row["seed"]) == seed
        and row["method"] == DEFAULT_CONTROL_METHOD
        and valid_primary_row(row)
    ]
    controls.sort(key=lambda item: int(item["within_seed_position"]))
    if len(controls) != 5:
        raise ValueError(f"seed {seed} must have five valid default controls")
    return controls


def _interpolate_control(controls: list[dict[str, Any]], position: int, objective: str) -> float:
    points = [
        (int(row["within_seed_position"]), float(row["objective_values"][objective]))
        for row in controls
    ]
    if position <= points[0][0]:
        return points[0][1]
    if position >= points[-1][0]:
        return points[-1][1]
    for (left_position, left_value), (right_position, right_value) in pairwise(points):
        if left_position <= position <= right_position:
            fraction = (position - left_position) / (right_position - left_position)
            return left_value + fraction * (right_value - left_value)
    raise RuntimeError("control interpolation failed")


def _candidate_contrasts(runs: list[dict[str, Any]], seeds: list[int]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for seed in seeds:
        controls = _controls(runs, seed)
        for method in PRIMARY_SEARCH_METHODS:
            slot = 0
            for row in _logical_rows(runs, seed, method):
                slot += 1
                if not valid_primary_row(row):
                    continue
                objectives = dict(row["objective_values"])
                position = int(row["within_seed_position"])
                control_tps = _interpolate_control(controls, position, "throughput_tps")
                control_p99 = _interpolate_control(controls, position, "p99_ms")
                relative_tps = float(objectives["throughput_tps"]) / control_tps - 1.0
                p99_delta = float(objectives["p99_ms"]) - control_p99
                if relative_tps > 0 and p99_delta < 0:
                    quadrant = "better_both"
                elif relative_tps > 0:
                    quadrant = "tps_only"
                elif p99_delta < 0:
                    quadrant = "p99_only"
                else:
                    quadrant = "worse_both"
                result.append(
                    {
                        "seed": seed,
                        "method": method,
                        "logical_slot": slot,
                        "physical_method": str(row["method"]),
                        "primary_run_id": str(row["primary_run_id"]),
                        "global_position": int(row["global_position"]),
                        "within_seed_position": position,
                        "throughput_tps": float(objectives["throughput_tps"]),
                        "p99_ms": float(objectives["p99_ms"]),
                        "interpolated_control_tps": control_tps,
                        "interpolated_control_p99_ms": control_p99,
                        "control_relative_tps": relative_tps,
                        "control_relative_p99_ms": p99_delta,
                        "quadrant": quadrant,
                    }
                )
    return result


def _quadrant_rows(
    contrasts: list[dict[str, Any]], analysis: dict[str, Any]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    frozen = {
        (int(row["seed"]), str(row["method"])): row for row in analysis["seed_method_results"]
    }
    for seed, method in sorted(
        {(int(row["seed"]), str(row["method"])) for row in contrasts},
        key=lambda item: (item[0], PRIMARY_SEARCH_METHODS.index(item[1])),
    ):
        selected = [row for row in contrasts if row["seed"] == seed and row["method"] == method]
        counts = {name: sum(row["quadrant"] == name for row in selected) for name in _QUADRANTS}
        terminal = frozen[(seed, method)]
        expected = {
            "better_both": int(terminal["observations_dominating_local_control"]),
            "tps_only": int(terminal["observations_beating_control_tps"])
            - int(terminal["observations_dominating_local_control"]),
            "p99_only": int(terminal["observations_beating_control_p99"])
            - int(terminal["observations_dominating_local_control"]),
        }
        expected["worse_both"] = int(terminal["valid_observations"]) - sum(expected.values())
        if counts != expected:
            raise ValueError(
                f"recomputed quadrant counts disagree with terminal analysis for {seed}/{method}"
            )
        result.append(
            {
                "seed": seed,
                "method": method,
                "valid_observations": len(selected),
                **counts,
                **{f"{key}_share": counts[key] / len(selected) for key in _QUADRANTS},
            }
        )
    return result


def _adaptive_gain_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trajectory in analysis["slot_trajectories"]:
        by_slot = {int(slot["slot"]): slot for slot in trajectory["slots"]}
        start = by_slot[12]
        end = by_slot[30]
        hypervolume_start = float(start["hypervolume"])
        hypervolume_end = float(end["hypervolume"])
        rows.append(
            {
                "seed": int(trajectory["seed"]),
                "method": str(trajectory["method"]),
                "hypervolume_slot_12": hypervolume_start,
                "hypervolume_slot_30": hypervolume_end,
                "hypervolume_gain": hypervolume_end - hypervolume_start,
                "hypervolume_gain_percent": (
                    100.0 * (hypervolume_end - hypervolume_start) / hypervolume_start
                    if hypervolume_start
                    else None
                ),
                "best_tps_slot_12": start["best_throughput_tps"],
                "best_tps_slot_30": end["best_throughput_tps"],
                "best_tps_change": float(end["best_throughput_tps"])
                - float(start["best_throughput_tps"]),
                "minimum_p99_slot_12": start["minimum_p99_ms"],
                "minimum_p99_slot_30": end["minimum_p99_ms"],
                "minimum_p99_change_ms": float(end["minimum_p99_ms"])
                - float(start["minimum_p99_ms"]),
            }
        )
    return rows


def _physical_fronts(runs: list[dict[str, Any]], seeds: list[int]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for seed in seeds:
        candidates = [
            row
            for row in runs
            if int(row["seed"]) == seed
            and row["method"] != DEFAULT_CONTROL_METHOD
            and valid_primary_row(row)
        ]
        front = [
            row
            for row in candidates
            if not any(
                (
                    float(other["objective_values"]["throughput_tps"])
                    >= float(row["objective_values"]["throughput_tps"])
                    and float(other["objective_values"]["p99_ms"])
                    <= float(row["objective_values"]["p99_ms"])
                )
                and (
                    float(other["objective_values"]["throughput_tps"])
                    > float(row["objective_values"]["throughput_tps"])
                    or float(other["objective_values"]["p99_ms"])
                    < float(row["objective_values"]["p99_ms"])
                )
                for other in candidates
            )
        ]
        maximum_tps = max(float(row["objective_values"]["throughput_tps"]) for row in front)
        minimum_p99 = min(float(row["objective_values"]["p99_ms"]) for row in front)
        controls = _controls(runs, seed)
        for row in sorted(
            front, key=lambda item: float(item["objective_values"]["throughput_tps"])
        ):
            objectives = dict(row["objective_values"])
            position = int(row["within_seed_position"])
            tps = float(objectives["throughput_tps"])
            p99 = float(objectives["p99_ms"])
            vector = [float(value) for value in row["candidate_vector"]]
            if len(vector) != len(PRIMARY_PARAMETER_ORDER):
                raise ValueError("Pareto candidate vector does not match the eight-knob space")
            result.append(
                {
                    "seed": seed,
                    "primary_run_id": str(row["primary_run_id"]),
                    "physical_method": str(row["method"]),
                    "within_seed_position": position,
                    "global_position": int(row["global_position"]),
                    "throughput_tps": tps,
                    "p99_ms": p99,
                    "control_relative_tps": tps
                    / _interpolate_control(controls, position, "throughput_tps")
                    - 1.0,
                    "control_relative_p99_ms": p99
                    - _interpolate_control(controls, position, "p99_ms"),
                    "extreme_maximum_tps": math.isclose(tps, maximum_tps),
                    "extreme_minimum_p99": math.isclose(p99, minimum_p99),
                    "normalized_configuration": {
                        name: value
                        for name, value in zip(PRIMARY_PARAMETER_ORDER, vector, strict=True)
                    },
                }
            )
    return result


def _competition_ranks(values: dict[str, float], higher_is_better: bool) -> dict[str, int]:
    ordered = sorted(
        values,
        key=lambda method: ((-values[method]) if higher_is_better else values[method], method),
    )
    result: dict[str, int] = {}
    previous: float | None = None
    previous_rank = 0
    for index, method in enumerate(ordered, start=1):
        value = values[method]
        rank = previous_rank if previous is not None and math.isclose(value, previous) else index
        result[method] = rank
        previous = value
        previous_rank = rank
    return result


def _consistency_rows(analysis: dict[str, Any], seeds: list[int]) -> list[dict[str, Any]]:
    source = list(analysis["seed_method_results"])
    result: list[dict[str, Any]] = []
    for metric, label, higher_is_better in _CONSISTENCY_METRICS:
        for seed in seeds:
            values: dict[str, float] = {}
            for method in PRIMARY_SEARCH_METHODS:
                row = next(
                    item
                    for item in source
                    if int(item["seed"]) == seed and item["method"] == method
                )
                values[method] = (
                    float(row["observations_dominating_local_control"])
                    / float(row["valid_observations"])
                    if metric == "local_control_domination_rate"
                    else float(row[metric])
                )
            ranks = _competition_ranks(values, higher_is_better)
            for method in PRIMARY_SEARCH_METHODS:
                result.append(
                    {
                        "metric": metric,
                        "metric_label": label,
                        "higher_is_better": higher_is_better,
                        "seed": seed,
                        "method": method,
                        "value": values[method],
                        "within_seed_rank": ranks[method],
                    }
                )
    return result


def _leave_one_seed_out_rows(
    consistency: list[dict[str, Any]], seeds: list[int]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    omissions: list[int | None] = [None, *seeds]
    for metric, label, higher_is_better in _CONSISTENCY_METRICS:
        metric_rows = [row for row in consistency if row["metric"] == metric]
        for omitted in omissions:
            values = {
                method: statistics.fmean(
                    float(row["value"])
                    for row in metric_rows
                    if row["method"] == method and row["seed"] != omitted
                )
                for method in PRIMARY_SEARCH_METHODS
            }
            ranks = _competition_ranks(values, higher_is_better)
            for method in PRIMARY_SEARCH_METHODS:
                result.append(
                    {
                        "metric": metric,
                        "metric_label": label,
                        "omitted_seed": omitted if omitted is not None else "none",
                        "retained_seed_count": len(seeds) - int(omitted is not None),
                        "method": method,
                        "mean_value": values[method],
                        "mean_rank": ranks[method],
                    }
                )
    return result


def _diversity_rows(
    runs: list[dict[str, Any]], seeds: list[int], analysis: dict[str, Any]
) -> list[dict[str, Any]]:
    terminal = {
        (int(row["seed"]), str(row["method"])): row for row in analysis["seed_method_results"]
    }
    result: list[dict[str, Any]] = []
    for seed in seeds:
        for method in PRIMARY_SEARCH_METHODS:
            logical = [row for row in _logical_rows(runs, seed, method) if valid_primary_row(row)]
            vectors = [tuple(float(value) for value in row["candidate_vector"]) for row in logical]
            nearest = [
                min(
                    math.dist(vector, other)
                    for other_index, other in enumerate(vectors)
                    if other_index != index
                )
                for index, vector in enumerate(vectors)
            ]
            endpoint = terminal[(seed, method)]
            result.append(
                {
                    "seed": seed,
                    "method": method,
                    "valid_candidates": len(vectors),
                    "median_nearest_neighbor_distance": statistics.median(nearest),
                    "local_control_domination_rate": float(
                        endpoint["observations_dominating_local_control"]
                    )
                    / float(endpoint["valid_observations"]),
                    "final_hypervolume": float(endpoint["hypervolume_at_0_negative_40"]),
                }
            )
    return result


def _elapsed_hours(row: dict[str, Any]) -> float | None:
    if not row.get("trial_started_at") or not row.get("trial_completed_at"):
        return None
    start = datetime.fromisoformat(str(row["trial_started_at"]).replace("Z", "+00:00"))
    end = datetime.fromisoformat(str(row["trial_completed_at"]).replace("Z", "+00:00"))
    return (end - start).total_seconds() / 3600.0


def _wall_clock_rows(runs: list[dict[str, Any]], seeds: list[int]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    physical_methods = [
        *PRIMARY_SEARCH_METHODS,
        SHARED_INITIAL_METHOD,
        DEFAULT_CONTROL_METHOD,
    ]
    for seed in seeds:
        for method in physical_methods:
            selected = [
                row
                for row in runs
                if int(row["seed"]) == seed and row["method"] == method and valid_primary_row(row)
            ]
            if not selected:
                continue
            elapsed = [value for row in selected if (value := _elapsed_hours(row)) is not None]
            restores = [
                float(row["restore_seconds"])
                for row in selected
                if row.get("restore_seconds") is not None
            ]
            result.append(
                {
                    "seed": seed,
                    "physical_method": method,
                    "completed_observations": len(selected),
                    "trial_hours_sum": sum(elapsed) if len(elapsed) == len(selected) else None,
                    "restore_seconds_median": statistics.median(restores) if restores else None,
                }
            )
    return result


def _summary_rows(
    analysis: dict[str, Any],
    gains: list[dict[str, Any]],
    fronts: list[dict[str, Any]],
    wall_clock: list[dict[str, Any]],
    seeds: list[int],
) -> list[dict[str, Any]]:
    endpoints = list(analysis["seed_method_results"])
    failures = {row["method"]: row for row in analysis["failure_accounting"]}
    shared_wall = {
        int(row["seed"]): row
        for row in wall_clock
        if row["physical_method"] == SHARED_INITIAL_METHOD
    }
    result: list[dict[str, Any]] = []
    for method in PRIMARY_SEARCH_METHODS:
        method_endpoints = [row for row in endpoints if row["method"] == method]
        method_gains = [row for row in gains if row["method"] == method]
        per_seed_front = {
            str(seed): sum(
                row["seed"] == seed and row["physical_method"] == method for row in fronts
            )
            for seed in seeds
        }
        shared_front = {
            str(seed): sum(
                row["seed"] == seed and row["physical_method"] == SHARED_INITIAL_METHOD
                for row in fronts
            )
            for seed in seeds
        }
        physical_wall = {
            int(row["seed"]): row for row in wall_clock if row["physical_method"] == method
        }
        allocated_hours: dict[str, float | None] = {}
        for seed in seeds:
            own = physical_wall.get(seed, {}).get("trial_hours_sum")
            shared = (
                shared_wall.get(seed, {}).get("trial_hours_sum") if method in BO_METHODS else 0.0
            )
            allocated_hours[str(seed)] = (
                float(own) + float(shared) if own is not None and shared is not None else None
            )
        accounting = failures[method]
        result.append(
            {
                "method": method,
                "mean_final_hypervolume": statistics.fmean(
                    float(row["hypervolume_at_0_negative_40"]) for row in method_endpoints
                ),
                "mean_slot_12_to_30_hypervolume_gain": statistics.fmean(
                    float(row["hypervolume_gain"]) for row in method_gains
                ),
                "mean_seed_best_tps": statistics.fmean(
                    float(row["best_throughput_tps"]) for row in method_endpoints
                ),
                "mean_seed_minimum_p99_ms": statistics.fmean(
                    float(row["minimum_p99_ms"]) for row in method_endpoints
                ),
                "local_control_domination_share": sum(
                    int(row["observations_dominating_local_control"]) for row in method_endpoints
                )
                / sum(int(row["valid_observations"]) for row in method_endpoints),
                "adaptive_physical_front_contributions_by_seed": per_seed_front,
                "shared_initial_physical_front_contributions_by_seed": (
                    shared_front if method in BO_METHODS else {str(seed): 0 for seed in seeds}
                ),
                "candidate_failed_slots": int(accounting["candidate_failed_slots"]),
                "infrastructure_exhausted_slots": int(accounting["infrastructure_exhausted_slots"]),
                "retained_infrastructure_attempts": int(
                    accounting["retained_infrastructure_attempts"]
                ),
                "allocated_trial_hours_by_seed": allocated_hours,
            }
        )
    return result


def secondary_tables(export: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Compute every D050 table from an authenticated terminal export."""
    analysis = dict(export["analysis"])
    runs = [dict(row) for row in export["history"]["runs"]]
    seeds = _seed_order(runs)
    contrasts = _candidate_contrasts(runs, seeds)
    quadrants = _quadrant_rows(contrasts, analysis)
    gains = _adaptive_gain_rows(analysis)
    fronts = _physical_fronts(runs, seeds)
    consistency = _consistency_rows(analysis, seeds)
    leave_one_out = _leave_one_seed_out_rows(consistency, seeds)
    diversity = _diversity_rows(runs, seeds, analysis)
    wall_clock = _wall_clock_rows(runs, seeds)
    summary = _summary_rows(analysis, gains, fronts, wall_clock, seeds)
    return {
        "s1-candidate-control-contrasts": contrasts,
        "s1-quadrant-shares": quadrants,
        "s2-adaptive-gain": gains,
        "s3-per-seed-physical-pareto": fronts,
        "s4-seed-consistency": consistency,
        "s4-leave-one-seed-out": leave_one_out,
        "s6-search-diversity": diversity,
        "s8-physical-wall-clock": wall_clock,
        "s8-summary-dashboard": summary,
    }


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def _format(value: object) -> str:
    if value is None:
        return "NA"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def _svg_document(width: int, height: int, title: str, body: list[str], note: str) -> str:
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            f"<title>{_escape(title)}</title>",
            '<rect width="100%" height="100%" fill="#ffffff"/>',
            f'<text x="52" y="34" font-family="sans-serif" font-size="20" font-weight="700" fill="#17202a">{_escape(title)}</text>',
            *body,
            f'<text x="52" y="{height - 18}" font-family="sans-serif" font-size="11" fill="#59636e">{_escape(note)}</text>',
            "</svg>",
            "",
        ]
    )


def _bounds(values: list[float], include_zero: bool = False) -> tuple[float, float]:
    selected = [*values, *([0.0] if include_zero else [])]
    low, high = min(selected), max(selected)
    if math.isclose(low, high):
        return low - 1.0, high + 1.0
    pad = 0.08 * (high - low)
    return low - pad, high + pad


def _scale(value: float, low: float, high: float, start: float, end: float) -> float:
    return start + (value - low) / (high - low) * (end - start)


def _method_legend(body: list[str], y: float, *, start_x: float = 65.0) -> None:
    for index, method in enumerate(PRIMARY_SEARCH_METHODS):
        x = start_x + index * 275
        body.append(
            f'<circle cx="{x}" cy="{y}" r="5" fill="{METHOD_COLORS[method]}"/>'
            f'<text x="{x + 10}" y="{y + 4}" font-family="sans-serif" font-size="10">'
            f"{_escape(METHOD_LABELS[method])}</text>"
        )


def _figure_s1(tables: dict[str, list[dict[str, Any]]]) -> str:
    points = tables["s1-candidate-control-contrasts"]
    shares = tables["s1-quadrant-shares"]
    seeds = list(dict.fromkeys(int(row["seed"]) for row in points))
    x_low, x_high = _bounds([float(row["control_relative_tps"]) for row in points], True)
    y_low, y_high = _bounds([float(row["control_relative_p99_ms"]) for row in points], True)
    body: list[str] = []
    for panel, seed in enumerate(seeds):
        top = 65 + panel * 320
        left, right, bottom = 80.0, 900.0, top + 220.0
        body.append(
            f'<text x="80" y="{top - 12}" font-family="sans-serif" font-size="14" font-weight="700">Seed {seed}</text>'
        )
        body.append(
            f'<rect x="{left}" y="{top}" width="{right - left}" height="220" fill="#fafbfc" stroke="#ccd2d8"/>'
        )
        zero_x = _scale(0.0, x_low, x_high, left, right)
        zero_y = _scale(0.0, y_low, y_high, bottom, float(top))
        body.extend(
            [
                f'<line x1="{_number(zero_x)}" y1="{top}" x2="{_number(zero_x)}" y2="{bottom}" stroke="#41464c" stroke-dasharray="4 3"/>',
                f'<line x1="{left}" y1="{_number(zero_y)}" x2="{right}" y2="{_number(zero_y)}" stroke="#41464c" stroke-dasharray="4 3"/>',
            ]
        )
        for row in points:
            if int(row["seed"]) != seed:
                continue
            x = _scale(float(row["control_relative_tps"]), x_low, x_high, left, right)
            y = _scale(float(row["control_relative_p99_ms"]), y_low, y_high, bottom, float(top))
            body.append(
                f'<circle cx="{_number(x)}" cy="{_number(y)}" r="3.2" fill="{METHOD_COLORS[str(row["method"])]}" fill-opacity="0.63"/>'
            )
        bar_left = 955.0
        seed_shares = [row for row in shares if int(row["seed"]) == seed]
        for index, row in enumerate(seed_shares):
            y = top + index * 40
            body.append(
                f'<text x="945" y="{y + 14}" text-anchor="end" font-family="sans-serif" font-size="11">{_escape(METHOD_LABELS[str(row["method"])])}</text>'
            )
            cursor = bar_left
            for quadrant in _QUADRANTS:
                width = 440.0 * float(row[f"{quadrant}_share"])
                body.append(
                    f'<rect x="{_number(cursor)}" y="{y}" width="{_number(width)}" height="20" fill="{_QUADRANT_COLORS[quadrant]}"/>'
                )
                cursor += width
    legend_x = 955
    for index, quadrant in enumerate(_QUADRANTS):
        x = legend_x + (index % 2) * 220
        y = 1015 + (index // 2) * 22
        body.append(
            f'<rect x="{x}" y="{y - 11}" width="12" height="12" fill="{_QUADRANT_COLORS[quadrant]}"/><text x="{x + 18}" y="{y}" font-family="sans-serif" font-size="11">{quadrant.replace("_", " ")}</text>'
        )
    _method_legend(body, 970)
    return _svg_document(
        1460,
        1085,
        "S1 - Candidate deltas from interpolated local controls",
        body,
        "Exploratory/descriptive only. x = TPS/control TPS - 1; y = p99 - control p99 (lower is better). Bars are per-seed shares.",
    )


def _figure_s2(tables: dict[str, list[dict[str, Any]]]) -> str:
    rows = tables["s2-adaptive-gain"]
    seeds = list(dict.fromkeys(int(row["seed"]) for row in rows))
    maximum = max(float(row["hypervolume_gain"]) for row in rows) or 1.0
    body: list[str] = []
    for seed_index, seed in enumerate(seeds):
        left = 70 + seed_index * 480
        body.append(
            f'<text x="{left}" y="68" font-family="sans-serif" font-size="15" font-weight="700">Seed {seed}</text>'
        )
        for method_index, method in enumerate(PRIMARY_SEARCH_METHODS):
            row = next(item for item in rows if item["seed"] == seed and item["method"] == method)
            y = 105 + method_index * 86
            width = 360 * float(row["hypervolume_gain"]) / maximum
            body.append(
                f'<text x="{left}" y="{y}" font-family="sans-serif" font-size="11">{_escape(METHOD_LABELS[method])}</text>'
            )
            body.append(
                f'<rect x="{left}" y="{y + 12}" width="360" height="22" fill="#eef1f4"/><rect x="{left}" y="{y + 12}" width="{_number(width)}" height="22" fill="{METHOD_COLORS[method]}"/>'
            )
            body.append(
                f'<text x="{left}" y="{y + 54}" font-family="monospace" font-size="10">HV {float(row["hypervolume_slot_12"]):.1f} -> {float(row["hypervolume_slot_30"]):.1f}; gain {float(row["hypervolume_gain"]):.1f} ({float(row["hypervolume_gain_percent"] or 0):.3f}%)</text>'
            )
    return _svg_document(
        1500,
        600,
        "S2 - Slot 12 to slot 30 hypervolume gain",
        body,
        "All five methods use the same slot-12-to-30 formula. Random and Sobol are shown symmetrically; this is not an adaptive-only causal contrast.",
    )


def _figure_s3(tables: dict[str, list[dict[str, Any]]]) -> str:
    rows = tables["s3-per-seed-physical-pareto"]
    seeds = list(dict.fromkeys(int(row["seed"]) for row in rows))
    x_low, x_high = _bounds([float(row["throughput_tps"]) for row in rows])
    y_low, y_high = _bounds([float(row["p99_ms"]) for row in rows])
    body: list[str] = []
    for panel, seed in enumerate(seeds):
        left = 70 + panel * 480
        right = left + 390
        top, bottom = 90.0, 420.0
        selected = [row for row in rows if int(row["seed"]) == seed]
        body.append(
            f'<text x="{left}" y="68" font-family="sans-serif" font-size="15" font-weight="700">Seed {seed}</text><rect x="{left}" y="{top}" width="390" height="330" fill="#fafbfc" stroke="#ccd2d8"/>'
        )
        ordered = sorted(selected, key=lambda row: float(row["throughput_tps"]))
        path = " ".join(
            f"{'M' if index == 0 else 'L'} {_number(_scale(float(row['throughput_tps']), x_low, x_high, left, right))} {_number(_scale(float(row['p99_ms']), y_low, y_high, bottom, top))}"
            for index, row in enumerate(ordered)
        )
        body.append(f'<path d="{path}" fill="none" stroke="#78828d"/>')
        for row in selected:
            x = _scale(float(row["throughput_tps"]), x_low, x_high, left, right)
            y = _scale(float(row["p99_ms"]), y_low, y_high, bottom, top)
            color = METHOD_COLORS.get(str(row["physical_method"]), "#8c8c8c")
            tags: list[str] = []
            if row["extreme_maximum_tps"]:
                tags.append("max TPS")
            if row["extreme_minimum_p99"]:
                tags.append("min p99")
            label = (
                f"{row['physical_method']} pos {row['within_seed_position']}; "
                f"TPS {float(row['throughput_tps']):.1f}; "
                f"p99 {float(row['p99_ms']):.3f}; "
                f"dTPS {float(row['control_relative_tps']):.4f}; "
                f"dp99 {float(row['control_relative_p99_ms']):.3f} ms"
            )
            body.append(
                f'<circle cx="{_number(x)}" cy="{_number(y)}" r="6" fill="{color}"><title>{_escape(label)}; normalized knobs {_escape(_format(row["normalized_configuration"]))}</title></circle>'
            )
            if tags:
                body.append(
                    f'<text x="{_number(x + 8)}" y="{_number(y - 8)}" font-family="sans-serif" font-size="10" font-weight="700">{_escape(" / ".join(tags))}</text>'
                )
    _method_legend(body, 470)
    return _svg_document(
        1500,
        540,
        "S3 - Per-seed physical nondominated candidates",
        body,
        "Only formula-free maximum-TPS and minimum-p99 extremes are tagged. Hover text carries method, position, deltas, and normalized eight-knob configuration. No pooled front or post-hoc knee.",
    )


def _figure_s4(tables: dict[str, list[dict[str, Any]]]) -> str:
    rows = tables["s4-seed-consistency"]
    seeds = list(dict.fromkeys(int(row["seed"]) for row in rows))
    body: list[str] = []
    for metric_index, (metric, label, _) in enumerate(_CONSISTENCY_METRICS):
        block_x = 50 + (metric_index % 2) * 740
        block_y = 70 + (metric_index // 2) * 300
        body.append(
            f'<text x="{block_x}" y="{block_y}" font-family="sans-serif" font-size="14" font-weight="700">{_escape(label)}</text>'
        )
        for seed_index, seed in enumerate(seeds):
            x = block_x + 270 + seed_index * 145
            body.append(
                f'<text x="{x + 64}" y="{block_y + 25}" text-anchor="middle" font-family="sans-serif" font-size="10">{seed}</text>'
            )
        for method_index, method in enumerate(PRIMARY_SEARCH_METHODS):
            y = block_y + 40 + method_index * 42
            body.append(
                f'<text x="{block_x + 260}" y="{y + 22}" text-anchor="end" font-family="sans-serif" font-size="10">{_escape(METHOD_LABELS[method])}</text>'
            )
            for seed_index, seed in enumerate(seeds):
                row = next(
                    item
                    for item in rows
                    if item["metric"] == metric
                    and item["seed"] == seed
                    and item["method"] == method
                )
                x = block_x + 270 + seed_index * 145
                shade = 245 - (6 - int(row["within_seed_rank"])) * 17
                fill = f"rgb({shade},{shade},{255})"
                body.append(
                    f'<rect x="{x}" y="{y}" width="128" height="34" fill="{fill}" stroke="#ffffff"/><text x="{x + 64}" y="{y + 21}" text-anchor="middle" font-family="monospace" font-size="10">{_escape(_format(row["value"]))} (r{row["within_seed_rank"]})</text>'
                )
    return _svg_document(
        1540,
        990,
        "S4 - Cross-seed values and within-seed ranks",
        body,
        "Each cell is value (within-seed rank). The companion CSV reports all-seed and leave-one-seed-out mean rankings; no composite rank is constructed.",
    )


def _ecdf_figure(rows: list[dict[str, Any]], key: str, title: str, axis_label: str) -> str:
    seeds = list(dict.fromkeys(int(row["seed"]) for row in rows))
    low, high = _bounds([float(row[key]) for row in rows], True)
    body: list[str] = []
    for panel, seed in enumerate(seeds):
        left = 65 + panel * 485
        right, top, bottom = left + 405, 95.0, 445.0
        zero = _scale(0.0, low, high, left, right)
        body.append(
            f'<text x="{left}" y="70" font-family="sans-serif" font-size="14" font-weight="700">Seed {seed}</text><rect x="{left}" y="{top}" width="405" height="350" fill="#fafbfc" stroke="#ccd2d8"/><line x1="{_number(zero)}" y1="{top}" x2="{_number(zero)}" y2="{bottom}" stroke="#333" stroke-dasharray="4 3"/>'
        )
        for method in PRIMARY_SEARCH_METHODS:
            values = sorted(
                float(row[key]) for row in rows if row["seed"] == seed and row["method"] == method
            )
            points = [
                f"{'M' if index == 1 else 'L'} {_number(_scale(value, low, high, left, right))} {_number(_scale(index / len(values), 0, 1, bottom, top))}"
                for index, value in enumerate(values, start=1)
            ]
            body.append(
                f'<path d="{" ".join(points)}" fill="none" stroke="{METHOD_COLORS[method]}" stroke-width="2"/>'
            )
            mean = statistics.fmean(values)
            x = _scale(mean, low, high, left, right)
            body.append(
                f'<circle cx="{_number(x)}" cy="{bottom + 10}" r="4" fill="{METHOD_COLORS[method]}"><title>{_escape(METHOD_LABELS[method])} seed mean {_format(mean)}</title></circle>'
            )
    body.append(
        f'<text x="750" y="480" text-anchor="middle" font-family="sans-serif" font-size="12">{_escape(axis_label)}</text>'
    )
    _method_legend(body, 515)
    return _svg_document(
        1500,
        575,
        title,
        body,
        "Candidate-level ECDFs are descriptive only. Colored markers below each panel are seed means; zero is the contemporaneous-control reference.",
    )


def _figure_s6(tables: dict[str, list[dict[str, Any]]]) -> str:
    rows = tables["s6-search-diversity"]
    body: list[str] = []
    for panel, (key, label) in enumerate(
        (
            ("local_control_domination_rate", "Local-control domination rate"),
            ("final_hypervolume", "Final hypervolume"),
        )
    ):
        selected_y = [float(row[key]) for row in rows]
        x_low, x_high = _bounds([float(row["median_nearest_neighbor_distance"]) for row in rows])
        y_low, y_high = _bounds(selected_y)
        left = 70 + panel * 730
        right, top, bottom = left + 620, 90.0, 470.0
        body.append(
            f'<text x="{left}" y="68" font-family="sans-serif" font-size="14" font-weight="700">{_escape(label)}</text><rect x="{left}" y="{top}" width="620" height="380" fill="#fafbfc" stroke="#ccd2d8"/>'
        )
        for row in rows:
            x = _scale(float(row["median_nearest_neighbor_distance"]), x_low, x_high, left, right)
            y = _scale(float(row[key]), y_low, y_high, bottom, top)
            body.append(
                f'<circle cx="{_number(x)}" cy="{_number(y)}" r="6" fill="{METHOD_COLORS[str(row["method"])]}"><title>seed {row["seed"]}; {_escape(METHOD_LABELS[str(row["method"])])}; spacing {_format(row["median_nearest_neighbor_distance"])}; outcome {_format(row[key])}</title></circle>'
            )
    _method_legend(body, 500)
    return _svg_document(
        1500,
        560,
        "S6 - Search diversity versus outcomes",
        body,
        "Appendix/explanatory only. Median nearest-neighbor distance is an exploration-exploitation illustration, not an effectiveness endpoint.",
    )


def _figure_s8(tables: dict[str, list[dict[str, Any]]]) -> str:
    rows = tables["s8-summary-dashboard"]
    columns: tuple[tuple[str, str, int], ...] = (
        ("method", "Method", 220),
        ("mean_final_hypervolume", "Mean final HV", 140),
        ("mean_slot_12_to_30_hypervolume_gain", "Mean HV gain", 130),
        ("mean_seed_best_tps", "Mean best TPS", 135),
        ("mean_seed_minimum_p99_ms", "Mean min p99", 130),
        ("local_control_domination_share", "Domination share", 145),
        ("adaptive_physical_front_contributions_by_seed", "Adaptive front / seed", 210),
        ("shared_initial_physical_front_contributions_by_seed", "Shared front / seed", 190),
        ("retained_infrastructure_attempts", "Infra retries", 105),
        ("allocated_trial_hours_by_seed", "Allocated hours / seed", 230),
    )
    body: list[str] = []
    x = 35
    for _, label, width in columns:
        body.append(
            f'<rect x="{x}" y="75" width="{width}" height="55" fill="#273746"/><text x="{x + 8}" y="104" font-family="sans-serif" font-size="10" font-weight="700" fill="#fff">{_escape(label)}</text>'
        )
        x += width
    for row_index, row in enumerate(rows):
        y = 130 + row_index * 72
        x = 35
        for key, _, width in columns:
            fill = "#f4f6f7" if row_index % 2 == 0 else "#ffffff"
            value: object = METHOD_LABELS[str(row[key])] if key == "method" else row[key]
            body.append(
                f'<rect x="{x}" y="{y}" width="{width}" height="72" fill="{fill}" stroke="#dde2e6"/><text x="{x + 8}" y="{y + 39}" font-family="sans-serif" font-size="10">{_escape(_format(value))}</text>'
            )
            x += width
    return _svg_document(
        1700,
        555,
        "S8 - Exploratory summary dashboard (values only)",
        body,
        "No composite score. BO hours allocate the physically measured shared initialization identically to each BO view; the shared block was incurred once physically.",
    )


_FIGURES: dict[str, Callable[[dict[str, list[dict[str, Any]]]], str]] = {
    "s1-local-control-quadrants.svg": _figure_s1,
    "s2-adaptive-gain.svg": _figure_s2,
    "s3-per-seed-pareto.svg": _figure_s3,
    "s4-cross-seed-consistency.svg": _figure_s4,
    "s5-control-relative-p99-ecdf.svg": lambda tables: _ecdf_figure(
        tables["s1-candidate-control-contrasts"],
        "control_relative_p99_ms",
        "S5 - Control-relative p99 distributions",
        "p99 - interpolated control p99 (ms; lower is better)",
    ),
    "s5-control-relative-tps-ecdf.svg": lambda tables: _ecdf_figure(
        tables["s1-candidate-control-contrasts"],
        "control_relative_tps",
        "S5 - Control-relative throughput distributions",
        "TPS / interpolated control TPS - 1 (higher is better)",
    ),
    "s6-diversity-vs-outcome.svg": _figure_s6,
    "s8-summary-dashboard.svg": _figure_s8,
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
        writer.writerow({column: _format(row.get(column)) for column in columns})
    return buffer.getvalue().encode("utf-8")


def _markdown(export: dict[str, Any], tables: dict[str, list[dict[str, Any]]]) -> str:
    drifted = [str(row["seed"]) for row in export["analysis"]["drift_flags"] if row["flagged"]]
    dashboard = tables["s8-summary-dashboard"]
    return "\n".join(
        [
            "# Wave A secondary showcase report",
            "",
            "**Evidence role: EXPLORATORY / DESCRIPTIVE ONLY.** The five D049 endpoints and "
            "the deterministic PRIMARY report remain the sole primary evidence. Seed is the "
            "only inferential unit; candidate rows are not independent replicates.",
            "",
            f"- Campaign: `{export['campaign_id']}`",
            f"- Primary block: `{export['primary_block_id']}`",
            f"- Analysis payload SHA-256: `{export['analysis_sha256']}`",
            f"- Terminal outcome: `{export['analysis']['outcome']}`",
            f"- Transparency-only drift flags: `{', '.join(drifted) if drifted else 'none'}`",
            "- S7 standalone wall-clock curve: rejected by D050; cost accounting appears only in S8.",
            "",
            "## Registered displays",
            "",
            *[f"- `figures/{name}`" for name in sorted(_FIGURES)],
            "",
            "## Mandatory interpretation guards",
            "",
            "- PostgreSQL-default comparisons use within-seed piecewise-linear interpolation "
            "of bracketing controls; the default is not a search method.",
            "- Bayesian throughput is parity, not dominance, against the local default; the "
            "raw-TPS extremes achieved by random/Sobol remain visible in S4 and S8.",
            "- The seed-88408573 drift flag is transparency-only and remains in this report.",
            "- Shared initialization is measured once physically. Logical BO views reuse its "
            "12 observations, while S8 either separates it or allocates it identically.",
            "- S3 has no knee tag and no pooled cross-seed Pareto front.",
            "- S4 reports endpoint-specific ranks and leave-one-seed-out sensitivity. It does "
            "not aggregate ranks into an overall winner.",
            "- No composite score is constructed or reported.",
            "- S6 is explanatory only and is not an effectiveness endpoint.",
            "",
            "## Compact values",
            "",
            "| method | mean final HV | mean HV gain 12-30 | mean best TPS | mean min p99 | domination share |",
            "|---|---:|---:|---:|---:|---:|",
            *[
                "| "
                + " | ".join(
                    [
                        METHOD_LABELS[str(row["method"])],
                        _format(row["mean_final_hypervolume"]),
                        _format(row["mean_slot_12_to_30_hypervolume_gain"]),
                        _format(row["mean_seed_best_tps"]),
                        _format(row["mean_seed_minimum_p99_ms"]),
                        _format(row["local_control_domination_share"]),
                    ]
                )
                + " |"
                for row in dashboard
            ],
            "",
        ]
    )


def _write(path: Path, data: bytes, root: Path, files: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    files.append(
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    )


def render_secondary_report(
    export: dict[str, Any],
    output_dir: Path,
    *,
    source_path: Path,
    source_file_sha256: str,
) -> dict[str, Any]:
    """Render byte-identical D050 showcases and return the report index summary."""
    root = output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    tables = secondary_tables(export)
    files: list[dict[str, Any]] = []
    _write(root / "secondary-report.md", _markdown(export, tables).encode(), root, files)
    for name, rows in sorted(tables.items()):
        _write(root / "tables" / f"{name}.csv", _csv_bytes(rows), root, files)
    for name, renderer in sorted(_FIGURES.items()):
        _write(root / "figures" / name, renderer(tables).encode(), root, files)
    index = {
        "report_kind": SECONDARY_REPORT_KIND,
        "evidence_role": SECONDARY_EVIDENCE_ROLE,
        "deterministic": True,
        "confirmatory_endpoints_changed": False,
        "candidate_rows_are_inferential_units": False,
        "inferential_unit": "seed",
        "source": {
            "path": str(source_path.resolve()),
            "file_sha256": source_file_sha256,
            "analysis_sha256": export["analysis_sha256"],
            "campaign_id": export["campaign_id"],
            "primary_block_id": export["primary_block_id"],
        },
        "rejected_display": "S7 standalone wall-clock curve",
        "table_row_counts": {name: len(rows) for name, rows in sorted(tables.items())},
        "figures": sorted(_FIGURES),
        "payload_sha256": _canonical_sha256(tables),
        "payload_sha256_semantics": "canonical JSON hash of all secondary tables",
        "files": files,
    }
    encoded = (json.dumps(index, indent=2, sort_keys=True) + "\n").encode()
    index_path = root / "secondary-report-index.json"
    index_path.write_bytes(encoded)
    return {
        "output_dir": str(root),
        "index_path": str(index_path),
        "index_file_sha256": hashlib.sha256(encoded).hexdigest(),
        "payload_sha256": index["payload_sha256"],
        "file_count": len(files) + 1,
        "total_bytes": sum(int(item["bytes"]) for item in files) + len(encoded),
        "files": files,
    }


def export_secondary_report(source_path: Path, output_dir: Path) -> dict[str, Any]:
    """Authenticate one terminal export and render its exploratory report tree."""
    export, source_hash = load_terminal_primary_export(source_path.resolve())
    return render_secondary_report(
        export,
        output_dir,
        source_path=source_path,
        source_file_sha256=source_hash,
    )
