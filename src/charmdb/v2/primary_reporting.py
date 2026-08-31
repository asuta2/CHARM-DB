"""Result-blind Wave A reporting: trajectories, tables, and deterministic figures.

Everything in this module is frozen before any PRIMARY observation exists. It
implements the outputs named in ``v2/docs/evidence-and-figures.md`` so that the
181.514-hour Wave A campaign is followed by rendering, not by writing analysis
code against visible results.

Rendering is deterministic: no timestamps, no locale-dependent formatting, and
no randomness. Re-rendering the same analysis reproduces byte-identical files.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from charmdb.v2.primary_design import BO_METHODS, CONTROL_POSITIONS
from charmdb.v2.primary_optimizer import (
    PRIMARY_REFERENCE_POINT,
    PrimaryObservation,
    primary_feasible_pareto,
    primary_hypervolume,
)
from charmdb.v2.protocol import PRIMARY_SEARCH_METHODS

SHARED_INITIAL_METHOD = "bo_shared_initial"
DEFAULT_CONTROL_METHOD = "postgresql_default"
LOGICAL_BUDGET = 30
SHARED_INITIAL_SIZE = 12

METHOD_LABELS: dict[str, str] = {
    "random": "Random",
    "sobol": "Sobol",
    "bo_qlognei_throughput": "qLogNEI (throughput)",
    "bo_qlognparego_multiobjective": "qLogNParEGO (multi-objective)",
    "bo_qlognehvi_multiobjective": "qLogNEHVI (multi-objective)",
}
METHOD_COLORS: dict[str, str] = {
    "random": "#4c72b0",
    "sobol": "#dd8452",
    "bo_qlognei_throughput": "#55a868",
    "bo_qlognparego_multiobjective": "#c44e52",
    "bo_qlognehvi_multiobjective": "#8172b3",
}
CONTROL_COLOR = "#4d4d4d"
STATUS_COLORS: dict[str, str] = {
    "COMPLETED": "#55a868",
    "CANDIDATE_FAILED": "#dd8452",
    "INFRASTRUCTURE_EXHAUSTED": "#c44e52",
}


# ---------------------------------------------------------------------------
# Ledger primitives
# ---------------------------------------------------------------------------


def valid_primary_row(row: dict[str, Any]) -> bool:
    """Return whether a durable primary run is a valid objective observation.

    This is the single definition used by analysis, trajectories, figures, and
    tables so that no surface can silently apply a looser rule.
    """
    objectives = dict(row.get("objective_values") or {})
    constraints = dict(row.get("constraint_values") or {})
    return (
        row.get("status") == "COMPLETED"
        and int(constraints.get("failures", 0)) == 0
        and math.isfinite(float(objectives.get("throughput_tps", math.nan)))
        and float(objectives.get("throughput_tps", 0)) > 0
        and math.isfinite(float(objectives.get("p99_ms", math.nan)))
        and float(objectives.get("p99_ms", 0)) > 0
    )


def _row_observation(row: dict[str, Any]) -> PrimaryObservation:
    objectives = dict(row["objective_values"])
    vector = row.get("candidate_vector") or []
    configuration = dict(row.get("requested_configuration") or {})
    return PrimaryObservation(
        uuid.UUID(str(row["primary_run_id"])),
        tuple(float(item) for item in vector),
        {str(key): str(value) for key, value in configuration.items()},
        float(objectives["throughput_tps"]),
        float(objectives["p99_ms"]),
        0,
        True,
    )


def _physical_source(method: str, slot: int) -> tuple[str, int]:
    """Map one logical candidate slot to the physical row that supplies it."""
    if method in BO_METHODS and slot <= SHARED_INITIAL_SIZE:
        return SHARED_INITIAL_METHOD, slot
    return method, slot


def _indexed_rows(rows: list[dict[str, Any]]) -> dict[tuple[int, str, int], dict[str, Any]]:
    index: dict[tuple[int, str, int], dict[str, Any]] = {}
    for row in rows:
        budget_position = row.get("budget_position")
        if budget_position is None:
            continue
        key = (int(row["seed"]), str(row["method"]), int(budget_position))
        if key in index:
            raise ValueError(f"duplicate primary ledger slot {key}")
        index[key] = row
    return index


@dataclass(frozen=True)
class SlotPoint:
    slot: int
    physical_method: str
    global_position: int
    within_seed_position: int
    status: str
    valid: bool
    infrastructure_attempts: int
    throughput_tps: float | None
    p99_ms: float | None
    best_throughput_tps: float | None
    minimum_p99_ms: float | None
    hypervolume: float


def logical_slot_trajectories(
    rows: list[dict[str, Any]], payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """Expand the physical ledger into per-seed, per-method 30-slot trajectories.

    A Bayesian method's slots 1-12 read the shared initialization observations
    once each; slots 13-30 read its own adaptive observations. Random and Sobol
    read their own 30 slots directly. A slot whose observation is invalid still
    consumes the slot and carries the previous best forward.
    """
    index = _indexed_rows(rows)
    trajectories: list[dict[str, Any]] = []
    for seed in payload["wave_a"]["seeds"]:
        for method in PRIMARY_SEARCH_METHODS:
            accumulated: list[PrimaryObservation] = []
            points: list[SlotPoint] = []
            for slot in range(1, LOGICAL_BUDGET + 1):
                physical_method, budget_position = _physical_source(method, slot)
                row = index.get((int(seed), physical_method, budget_position))
                if row is None:
                    raise ValueError(
                        f"primary ledger is missing seed {seed} {physical_method} "
                        f"slot {budget_position}"
                    )
                valid = valid_primary_row(row)
                if valid:
                    accumulated.append(_row_observation(row))
                objectives = dict(row.get("objective_values") or {})
                points.append(
                    SlotPoint(
                        slot=slot,
                        physical_method=physical_method,
                        global_position=int(row["global_position"]),
                        within_seed_position=int(row["within_seed_position"]),
                        status=str(row["status"]),
                        valid=valid,
                        infrastructure_attempts=int(row.get("infrastructure_attempts") or 0),
                        throughput_tps=float(objectives["throughput_tps"]) if valid else None,
                        p99_ms=float(objectives["p99_ms"]) if valid else None,
                        best_throughput_tps=(
                            max(item.throughput_tps for item in accumulated)
                            if accumulated
                            else None
                        ),
                        minimum_p99_ms=(
                            min(item.p99_ms for item in accumulated) if accumulated else None
                        ),
                        hypervolume=primary_hypervolume(accumulated),
                    )
                )
            trajectories.append(
                {
                    "seed": int(seed),
                    "method": method,
                    "slots": [asdict(point) for point in points],
                    "valid_slots": sum(point.valid for point in points),
                    "final_hypervolume": points[-1].hypervolume,
                }
            )
    return trajectories


def failure_accounting(rows: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Count terminal statuses and retained infrastructure attempts per method.

    Shared initialization rows are attributed to every Bayesian method because
    each Bayesian 30-slot budget contains them, exactly as the trajectory
    expansion does. Default controls are reported on their own physical row and
    never as a method.
    """
    index = _indexed_rows(rows)
    accounting: list[dict[str, Any]] = []
    for method in PRIMARY_SEARCH_METHODS:
        completed = 0
        candidate_failed = 0
        infrastructure_exhausted = 0
        attempts = 0
        retried_slots = 0
        invalid_completed = 0
        for seed in payload["wave_a"]["seeds"]:
            for slot in range(1, LOGICAL_BUDGET + 1):
                physical_method, budget_position = _physical_source(method, slot)
                row = index[(int(seed), physical_method, budget_position)]
                status = str(row["status"])
                if status == "COMPLETED":
                    completed += 1
                    if not valid_primary_row(row):
                        invalid_completed += 1
                elif status == "CANDIDATE_FAILED":
                    candidate_failed += 1
                elif status == "INFRASTRUCTURE_EXHAUSTED":
                    infrastructure_exhausted += 1
                else:
                    raise ValueError(f"non-terminal primary run status {status!r} in analysis")
                slot_attempts = int(row.get("infrastructure_attempts") or 0)
                attempts += slot_attempts
                retried_slots += int(slot_attempts > 0)
        accounting.append(
            {
                "method": method,
                "logical_slots": LOGICAL_BUDGET * len(payload["wave_a"]["seeds"]),
                "completed_slots": completed,
                "valid_candidate_observations": completed - invalid_completed,
                "completed_but_invalid_slots": invalid_completed,
                "candidate_failed_slots": candidate_failed,
                "infrastructure_exhausted_slots": infrastructure_exhausted,
                "retained_infrastructure_attempts": attempts,
                "slots_with_infrastructure_retry": retried_slots,
            }
        )
    controls = [row for row in rows if row["method"] == DEFAULT_CONTROL_METHOD]
    accounting.append(
        {
            "method": DEFAULT_CONTROL_METHOD,
            "logical_slots": 0,
            "completed_slots": sum(row["status"] == "COMPLETED" for row in controls),
            "valid_candidate_observations": sum(valid_primary_row(row) for row in controls),
            "completed_but_invalid_slots": sum(
                row["status"] == "COMPLETED" and not valid_primary_row(row) for row in controls
            ),
            "candidate_failed_slots": sum(row["status"] == "CANDIDATE_FAILED" for row in controls),
            "infrastructure_exhausted_slots": sum(
                row["status"] == "INFRASTRUCTURE_EXHAUSTED" for row in controls
            ),
            "retained_infrastructure_attempts": sum(
                int(row.get("infrastructure_attempts") or 0) for row in controls
            ),
            "slots_with_infrastructure_retry": sum(
                int(row.get("infrastructure_attempts") or 0) > 0 for row in controls
            ),
        }
    )
    return accounting


def pareto_front(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the nondominated valid candidates across the whole wave."""
    candidates = [
        row for row in rows if row["method"] != DEFAULT_CONTROL_METHOD and valid_primary_row(row)
    ]
    by_id = {str(row["primary_run_id"]): row for row in candidates}
    front = primary_feasible_pareto([_row_observation(row) for row in candidates])
    result: list[dict[str, Any]] = []
    for observation in front:
        row = by_id[str(observation.run_id)]
        result.append(
            {
                "primary_run_id": str(observation.run_id),
                "seed": int(row["seed"]),
                "physical_method": str(row["method"]),
                "shared_with_methods": [
                    str(item) for item in (row.get("shared_with_methods") or [])
                ],
                "global_position": int(row["global_position"]),
                "throughput_tps": observation.throughput_tps,
                "p99_ms": observation.p99_ms,
                "requested_configuration": {
                    str(key): str(value)
                    for key, value in dict(row.get("requested_configuration") or {}).items()
                },
            }
        )
    return result


def control_series(rows: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every default control in chronological physical order."""
    series: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: int(item["global_position"])):
        if row["method"] != DEFAULT_CONTROL_METHOD:
            continue
        valid = valid_primary_row(row)
        objectives = dict(row.get("objective_values") or {})
        series.append(
            {
                "seed": int(row["seed"]),
                "global_position": int(row["global_position"]),
                "within_seed_position": int(row["within_seed_position"]),
                "status": str(row["status"]),
                "valid": valid,
                "throughput_tps": float(objectives["throughput_tps"]) if valid else None,
                "p99_ms": float(objectives["p99_ms"]) if valid else None,
            }
        )
    expected = len(payload["wave_a"]["seeds"]) * len(CONTROL_POSITIONS)
    if len(series) != expected:
        raise ValueError(f"primary ledger must contain exactly {expected} default controls")
    return series


def candidate_scatter(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return every valid candidate as a labelled TPS/p99 point."""
    points: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: int(item["global_position"])):
        if row["method"] == DEFAULT_CONTROL_METHOD or not valid_primary_row(row):
            continue
        objectives = dict(row["objective_values"])
        points.append(
            {
                "seed": int(row["seed"]),
                "physical_method": str(row["method"]),
                "global_position": int(row["global_position"]),
                "throughput_tps": float(objectives["throughput_tps"]),
                "p99_ms": float(objectives["p99_ms"]),
            }
        )
    return points


# ---------------------------------------------------------------------------
# Deterministic SVG primitives
# ---------------------------------------------------------------------------


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def _tick_label(value: float) -> str:
    magnitude = abs(value)
    if magnitude >= 1000:
        return f"{value:.0f}"
    if magnitude >= 10:
        return f"{value:.1f}"
    if magnitude >= 1:
        return f"{value:.2f}"
    return f"{value:.4g}"


@dataclass(frozen=True)
class _Frame:
    """A padded plot area with linear scales in SVG pixel space."""

    left: float
    right: float
    top: float
    bottom: float
    x_low: float
    x_high: float
    y_low: float
    y_high: float

    def x(self, value: float) -> float:
        span = self.x_high - self.x_low
        fraction = 0.5 if span == 0 else (value - self.x_low) / span
        return self.left + (self.right - self.left) * fraction

    def y(self, value: float) -> float:
        span = self.y_high - self.y_low
        fraction = 0.5 if span == 0 else (value - self.y_low) / span
        return self.bottom - (self.bottom - self.top) * fraction


def _padded_bounds(values: list[float], pad_fraction: float = 0.06) -> tuple[float, float]:
    if not values:
        return 0.0, 1.0
    low, high = min(values), max(values)
    if low == high:
        pad = abs(low) * pad_fraction or 1.0
        return low - pad, high + pad
    pad = (high - low) * pad_fraction
    return low - pad, high + pad


def _axes(frame: _Frame, x_title: str, y_title: str, ticks: int = 5) -> list[str]:
    parts = [
        f'<rect x="{_number(frame.left)}" y="{_number(frame.top)}" '
        f'width="{_number(frame.right - frame.left)}" '
        f'height="{_number(frame.bottom - frame.top)}" fill="#fbfbfc" stroke="#d0d3d8"/>'
    ]
    for index in range(ticks):
        fraction = index / (ticks - 1)
        value = frame.x_low + (frame.x_high - frame.x_low) * fraction
        x = frame.x(value)
        parts.append(
            f'<line x1="{_number(x)}" y1="{_number(frame.top)}" x2="{_number(x)}" '
            f'y2="{_number(frame.bottom)}" stroke="#e7e9ec"/>'
        )
        parts.append(
            f'<text x="{_number(x)}" y="{_number(frame.bottom + 18)}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="11" fill="#33373d">'
            f"{_escape(_tick_label(value))}</text>"
        )
        value = frame.y_low + (frame.y_high - frame.y_low) * fraction
        y = frame.y(value)
        parts.append(
            f'<line x1="{_number(frame.left)}" y1="{_number(y)}" '
            f'x2="{_number(frame.right)}" y2="{_number(y)}" stroke="#e7e9ec"/>'
        )
        parts.append(
            f'<text x="{_number(frame.left - 8)}" y="{_number(y + 4)}" text-anchor="end" '
            f'font-family="sans-serif" font-size="11" fill="#33373d">'
            f"{_escape(_tick_label(value))}</text>"
        )
    middle_x = (frame.left + frame.right) / 2
    middle_y = (frame.top + frame.bottom) / 2
    parts.append(
        f'<text x="{_number(middle_x)}" y="{_number(frame.bottom + 40)}" text-anchor="middle" '
        f'font-family="sans-serif" font-size="13" fill="#1c1f24">{_escape(x_title)}</text>'
    )
    parts.append(
        f'<text x="22" y="{_number(middle_y)}" text-anchor="middle" '
        f'transform="rotate(-90 22 {_number(middle_y)})" font-family="sans-serif" '
        f'font-size="13" fill="#1c1f24">{_escape(y_title)}</text>'
    )
    return parts


def _legend(entries: list[tuple[str, str]], x: float, y: float) -> list[str]:
    parts: list[str] = []
    for index, (label, color) in enumerate(entries):
        offset = y + index * 18
        parts.append(
            f'<rect x="{_number(x)}" y="{_number(offset - 9)}" width="12" height="12" '
            f'fill="{color}" stroke="#33373d" stroke-width="0.5"/>'
        )
        parts.append(
            f'<text x="{_number(x + 18)}" y="{_number(offset + 1)}" font-family="sans-serif" '
            f'font-size="12" fill="#1c1f24">{_escape(label)}</text>'
        )
    return parts


def _document(width: int, height: int, title: str, body: list[str], caption: str) -> str:
    header = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img">',
        f"<title>{_escape(title)}</title>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{_number(width / 2)}" y="28" text-anchor="middle" font-family="sans-serif" '
        f'font-size="17" fill="#1c1f24">{_escape(title)}</text>',
    ]
    footer = [
        f'<text x="{_number(width / 2)}" y="{height - 10}" text-anchor="middle" '
        f'font-family="sans-serif" font-size="11" fill="#5b6069">{_escape(caption)}</text>',
        "</svg>",
    ]
    return "\n".join(header + body + footer) + "\n"


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------


def figure_pareto(analysis: dict[str, Any]) -> str:
    """TPS versus p99 with every valid candidate, the front, and the controls."""
    points = list(analysis["candidate_scatter"])
    controls = [item for item in analysis["control_series"] if item["valid"]]
    front = list(analysis["pareto_front"])
    width, height = 960, 600
    tps_values = [float(item["throughput_tps"]) for item in points] + [
        float(item["throughput_tps"]) for item in controls
    ]
    p99_values = [float(item["p99_ms"]) for item in points] + [
        float(item["p99_ms"]) for item in controls
    ]
    x_low, x_high = _padded_bounds(tps_values)
    y_low, y_high = _padded_bounds(p99_values)
    frame = _Frame(80, width - 300, 52, height - 70, x_low, x_high, y_low, y_high)
    body = _axes(frame, "Successful throughput (TPS)", "p99 latency (ms, lower is better)")
    for item in points:
        method = str(item["physical_method"])
        colors = (
            [METHOD_COLORS[name] for name in BO_METHODS]
            if method == SHARED_INITIAL_METHOD
            else [METHOD_COLORS[method]]
        )
        x = frame.x(float(item["throughput_tps"]))
        y = frame.y(float(item["p99_ms"]))
        if len(colors) == 1:
            body.append(
                f'<circle cx="{_number(x)}" cy="{_number(y)}" r="4" fill="{colors[0]}" '
                f'fill-opacity="0.72" stroke="#ffffff" stroke-width="0.6"/>'
            )
        else:
            body.append(
                f'<rect x="{_number(x - 4)}" y="{_number(y - 4)}" width="8" height="8" '
                f'fill="#7f7f7f" fill-opacity="0.78" stroke="#ffffff" stroke-width="0.6"/>'
            )
    if len(front) >= 2:
        coordinates = " ".join(
            f"{_number(frame.x(float(item['throughput_tps'])))},"
            f"{_number(frame.y(float(item['p99_ms'])))}"
            for item in front
        )
        body.append(
            f'<polyline fill="none" stroke="#1c1f24" stroke-width="1.6" '
            f'stroke-dasharray="6 4" points="{coordinates}"/>'
        )
    for item in front:
        x = frame.x(float(item["throughput_tps"]))
        y = frame.y(float(item["p99_ms"]))
        body.append(
            f'<circle cx="{_number(x)}" cy="{_number(y)}" r="6.5" fill="none" '
            f'stroke="#1c1f24" stroke-width="1.4"/>'
        )
    for item in controls:
        x = frame.x(float(item["throughput_tps"]))
        y = frame.y(float(item["p99_ms"]))
        body.append(
            f'<path d="M {_number(x - 5)} {_number(y)} L {_number(x)} {_number(y - 5)} '
            f'L {_number(x + 5)} {_number(y)} L {_number(x)} {_number(y + 5)} Z" '
            f'fill="none" stroke="{CONTROL_COLOR}" stroke-width="1.6"/>'
        )
    legend = [(METHOD_LABELS[method], METHOD_COLORS[method]) for method in PRIMARY_SEARCH_METHODS]
    legend.append(("Shared BO initialization", "#7f7f7f"))
    legend.append(("PostgreSQL default control", CONTROL_COLOR))
    body.extend(_legend(legend, width - 280, 90))
    return _document(
        width,
        height,
        "Wave A candidates, nondominated front, and interleaved default controls",
        body,
        "Controls are overlaid and never joined to a method. Shared BO initialization points are "
        "measured once and counted in each Bayesian budget.",
    )


def _slot_series(
    analysis: dict[str, Any], key: str
) -> tuple[dict[str, list[tuple[int, list[float]]]], list[float]]:
    per_method: dict[str, list[tuple[int, list[float]]]] = {}
    observed: list[float] = []
    for trajectory in analysis["slot_trajectories"]:
        method = str(trajectory["method"])
        values: list[float] = []
        for slot in trajectory["slots"]:
            value = slot[key]
            values.append(math.nan if value is None else float(value))
            if value is not None:
                observed.append(float(value))
        per_method.setdefault(method, []).append((int(trajectory["seed"]), values))
    return per_method, observed


def _figure_by_slot(analysis: dict[str, Any], key: str, title: str, y_title: str, note: str) -> str:
    per_method, observed = _slot_series(analysis, key)
    width, height = 960, 560
    y_low, y_high = _padded_bounds(observed)
    frame = _Frame(84, width - 300, 52, height - 70, 1.0, float(LOGICAL_BUDGET), y_low, y_high)
    body = _axes(frame, "Logical candidate slot (1-30)", y_title)
    for method in PRIMARY_SEARCH_METHODS:
        color = METHOD_COLORS[method]
        seeds = sorted(per_method.get(method, []), key=lambda item: item[0])
        for _, values in seeds:
            coordinates = " ".join(
                f"{_number(frame.x(index + 1))},{_number(frame.y(value))}"
                for index, value in enumerate(values)
                if not math.isnan(value)
            )
            if coordinates:
                body.append(
                    f'<polyline fill="none" stroke="{color}" stroke-width="1" '
                    f'stroke-opacity="0.38" points="{coordinates}"/>'
                )
        aggregate: list[str] = []
        for index in range(LOGICAL_BUDGET):
            defined = [values[index] for _, values in seeds if not math.isnan(values[index])]
            if len(defined) == len(seeds) and defined:
                mean = sum(defined) / len(defined)
                aggregate.append(f"{_number(frame.x(index + 1))},{_number(frame.y(mean))}")
        if aggregate:
            body.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="2.6" '
                f'points="{" ".join(aggregate)}"/>'
            )
    body.extend(
        _legend(
            [(METHOD_LABELS[method], METHOD_COLORS[method]) for method in PRIMARY_SEARCH_METHODS],
            width - 280,
            96,
        )
    )
    return _document(width, height, title, body, note)


def figure_hypervolume_by_slot(analysis: dict[str, Any]) -> str:
    return _figure_by_slot(
        analysis,
        "hypervolume",
        "Fixed-reference hypervolume by logical candidate slot",
        f"Hypervolume at reference {tuple(PRIMARY_REFERENCE_POINT)}",
        "Thin lines are individual seeds; the bold line is the across-seed mean at n=3.",
    )


def figure_best_throughput_by_slot(analysis: dict[str, Any]) -> str:
    return _figure_by_slot(
        analysis,
        "best_throughput_tps",
        "Best observed throughput by logical candidate slot",
        "Best observed TPS so far",
        "Thin lines are individual seeds; the bold line is the across-seed mean at n=3.",
    )


def figure_minimum_p99_by_slot(analysis: dict[str, Any]) -> str:
    return _figure_by_slot(
        analysis,
        "minimum_p99_ms",
        "Minimum observed p99 by logical candidate slot",
        "Minimum observed p99 (ms, lower is better)",
        "Thin lines are individual seeds; the bold line is the across-seed mean at n=3.",
    )


def figure_default_drift(analysis: dict[str, Any]) -> str:
    """Default controls over chronological physical position, separated by seed."""
    series = [item for item in analysis["control_series"] if item["valid"]]
    seeds = sorted({int(item["seed"]) for item in analysis["control_series"]})
    width, height = 960, 660
    positions = [float(item["global_position"]) for item in analysis["control_series"]]
    x_low, x_high = (1.0, 393.0) if not positions else (1.0, max(393.0, max(positions)))
    tps_low, tps_high = _padded_bounds([float(item["throughput_tps"]) for item in series])
    p99_low, p99_high = _padded_bounds([float(item["p99_ms"]) for item in series])
    top = _Frame(84, width - 260, 56, 320, x_low, x_high, tps_low, tps_high)
    bottom = _Frame(84, width - 260, 388, height - 70, x_low, x_high, p99_low, p99_high)
    body = _axes(top, "Chronological physical observation (1-393)", "Default control TPS")
    body.extend(
        _axes(bottom, "Chronological physical observation (1-393)", "Default control p99 (ms)")
    )
    palette = ["#4c72b0", "#dd8452", "#55a868"]
    for index, seed in enumerate(seeds):
        color = palette[index % len(palette)]
        seed_points = [item for item in series if int(item["seed"]) == seed]
        for frame, key in ((top, "throughput_tps"), (bottom, "p99_ms")):
            coordinates = " ".join(
                f"{_number(frame.x(float(item['global_position'])))},"
                f"{_number(frame.y(float(item[key])))}"
                for item in seed_points
            )
            if coordinates:
                body.append(
                    f'<polyline fill="none" stroke="{color}" stroke-width="2" '
                    f'points="{coordinates}"/>'
                )
            for item in seed_points:
                body.append(
                    f'<circle cx="{_number(frame.x(float(item["global_position"])))}" '
                    f'cy="{_number(frame.y(float(item[key])))}" r="4" fill="{color}"/>'
                )
    body.extend(
        _legend(
            [(f"seed {seed}", palette[index % len(palette)]) for index, seed in enumerate(seeds)],
            width - 240,
            96,
        )
    )
    flagged = [item for item in analysis["drift_flags"] if item.get("flagged")]
    note = (
        "Drift flags are transparency markers only and never terminate or discard Wave A."
        if not flagged
        else f"{len(flagged)} seed(s) raised a transparency drift flag; Wave A is not discarded."
    )
    return _document(width, height, "PostgreSQL default control drift within each seed", body, note)


def figure_safety_outcomes(analysis: dict[str, Any]) -> str:
    """Grouped terminal-status counts per method, including retained retries."""
    accounting = [
        item for item in analysis["failure_accounting"] if item["method"] in METHOD_LABELS
    ]
    statuses = ["COMPLETED", "CANDIDATE_FAILED", "INFRASTRUCTURE_EXHAUSTED"]
    keys = {
        "COMPLETED": "completed_slots",
        "CANDIDATE_FAILED": "candidate_failed_slots",
        "INFRASTRUCTURE_EXHAUSTED": "infrastructure_exhausted_slots",
    }
    width, height = 960, 520
    maximum = max([float(item[keys[status]]) for item in accounting for status in statuses] + [1.0])
    frame = _Frame(84, width - 300, 56, height - 90, 0.0, float(len(accounting)), 0.0, maximum)
    body = _axes(frame, "Search method", "Logical slots (90 per method)")
    group_width = (frame.right - frame.left) / max(1, len(accounting))
    for index, item in enumerate(accounting):
        for offset, status in enumerate(statuses):
            value = float(item[keys[status]])
            bar_width = group_width * 0.22
            x = frame.left + group_width * (index + 0.17) + offset * bar_width
            y = frame.y(value)
            body.append(
                f'<rect x="{_number(x)}" y="{_number(y)}" width="{_number(bar_width * 0.88)}" '
                f'height="{_number(frame.bottom - y)}" fill="{STATUS_COLORS[status]}"/>'
            )
        body.append(
            f'<text x="{_number(frame.left + group_width * (index + 0.5))}" '
            f'y="{_number(frame.bottom + 34)}" text-anchor="middle" font-family="sans-serif" '
            f'font-size="11" fill="#1c1f24">{_escape(METHOD_LABELS[str(item["method"])])}</text>'
        )
    body.extend(
        _legend(
            [(status.replace("_", " ").title(), STATUS_COLORS[status]) for status in statuses],
            width - 280,
            96,
        )
    )
    retries = sum(int(item["retained_infrastructure_attempts"]) for item in accounting)
    return _document(
        width,
        height,
        "Terminal slot outcomes by method",
        body,
        f"{retries} retained infrastructure attempts consume no candidate slot and never train "
        "the optimizer.",
    )


FIGURES: dict[str, Any] = {
    "figure-pareto-tps-vs-p99.svg": figure_pareto,
    "figure-hypervolume-by-slot.svg": figure_hypervolume_by_slot,
    "figure-best-throughput-by-slot.svg": figure_best_throughput_by_slot,
    "figure-minimum-p99-by-slot.svg": figure_minimum_p99_by_slot,
    "figure-default-drift-by-position.svg": figure_default_drift,
    "figure-safety-outcomes-by-method.svg": figure_safety_outcomes,
}


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def primary_tables(analysis: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Build every predefined Wave A table as ordered row dictionaries."""
    accounting = {str(item["method"]): item for item in analysis["failure_accounting"]}
    method_outcomes: list[dict[str, Any]] = []
    for summary in analysis["method_results"]:
        method = str(summary["method"])
        counts = accounting[method]
        method_outcomes.append(
            {
                "method": method,
                "label": METHOD_LABELS[method],
                "seeds": summary["seed_count"],
                "logical_slots": counts["logical_slots"],
                "valid_candidate_observations": counts["valid_candidate_observations"],
                "candidate_failed_slots": counts["candidate_failed_slots"],
                "completed_but_invalid_slots": counts["completed_but_invalid_slots"],
                "infrastructure_exhausted_slots": counts["infrastructure_exhausted_slots"],
                "retained_infrastructure_attempts": counts["retained_infrastructure_attempts"],
                "mean_final_hypervolume": summary["mean_final_hypervolume"],
                "mean_seed_best_throughput_tps": summary["mean_seed_best_throughput_tps"],
                "mean_seed_minimum_p99_ms": summary["mean_seed_minimum_p99_ms"],
                "mean_control_relative_tps": summary["mean_control_relative_tps"],
                "mean_control_relative_p99_ms": summary["mean_control_relative_p99_ms"],
            }
        )
    slot_rows: list[dict[str, Any]] = []
    for trajectory in analysis["slot_trajectories"]:
        for slot in trajectory["slots"]:
            slot_rows.append(
                {
                    "seed": trajectory["seed"],
                    "method": trajectory["method"],
                    "slot": slot["slot"],
                    "physical_method": slot["physical_method"],
                    "global_position": slot["global_position"],
                    "status": slot["status"],
                    "valid": slot["valid"],
                    "throughput_tps": slot["throughput_tps"],
                    "p99_ms": slot["p99_ms"],
                    "best_throughput_tps": slot["best_throughput_tps"],
                    "minimum_p99_ms": slot["minimum_p99_ms"],
                    "hypervolume": slot["hypervolume"],
                }
            )
    return {
        "method-outcomes": method_outcomes,
        "seed-method-outcomes": list(analysis["seed_method_results"]),
        "seed-level-statistics": list(analysis["seed_level_statistics"]),
        "pairwise-contrasts": list(analysis["pairwise_comparisons"]),
        "safety-and-failures": list(analysis["failure_accounting"]),
        "default-controls": list(analysis["control_series"]),
        "default-drift": list(analysis["drift_flags"]),
        "pareto-front": list(analysis["pareto_front"]),
        "slot-trajectories": slot_rows,
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
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _cell(row.get(column)) for column in columns})
    return buffer.getvalue().encode("utf-8")


def _markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    lines = ["| " + " | ".join(columns) + " |", "|" + "|".join(["---"] * len(columns)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_cell(row.get(column)) for column in columns) + " |")
    return "\n".join(lines)


def _round(value: Any, digits: int = 4) -> Any:
    """Round floats for display without turning counts or flags into floats."""
    if isinstance(value, bool) or not isinstance(value, float):
        return value
    return round(value, digits)


def render_markdown(analysis: dict[str, Any], context: dict[str, Any]) -> str:
    tables = primary_tables(analysis)
    lines: list[str] = [
        "# Protocol-v2 Wave A primary comparison report",
        "",
        f"- Campaign: `{context['campaign_id']}`",
        f"- Primary block: `{context['primary_block_id']}`",
        f"- Manifest SHA-256: `{context['manifest_sha256']}`",
        f"- Schedule SHA-256: `{context['schedule_sha256']}`",
        f"- Candidate-design SHA-256: `{context['candidate_design_sha256']}`",
        f"- Analysis payload SHA-256: `{context['analysis_sha256']}`",
        f"- Benchmark profile: `{context['benchmark_profile_id']}`",
        f"- Hypervolume reference: `{tuple(PRIMARY_REFERENCE_POINT)}`",
        f"- Outcome: **{analysis['outcome']}**",
        "",
        "Wave A is reported on its own. It is a three-seed comparison and must never be "
        "described as five-seed confirmation. The PostgreSQL default is an interleaved "
        "reference, not a method row, and consumes no candidate slot.",
        "",
        "## Method outcomes",
        "",
        _markdown_table(
            [
                {key: _round(value) for key, value in row.items()}
                for row in tables["method-outcomes"]
            ],
            [
                "label",
                "logical_slots",
                "valid_candidate_observations",
                "candidate_failed_slots",
                "infrastructure_exhausted_slots",
                "retained_infrastructure_attempts",
                "mean_final_hypervolume",
                "mean_seed_best_throughput_tps",
                "mean_seed_minimum_p99_ms",
                "mean_control_relative_tps",
                "mean_control_relative_p99_ms",
            ],
        ),
        "",
        "## Seed-level endpoint summaries",
        "",
        _markdown_table(
            [
                {
                    "metric": row["metric"],
                    "direction": row["direction"],
                    "method": METHOD_LABELS.get(str(row["method"]), str(row["method"])),
                    "n": row["n"],
                    "mean": _round(row["mean"]),
                    "median": _round(row["median"]),
                    "sd": _round(row["standard_deviation"]),
                    "mad": _round(row["median_absolute_deviation"]),
                    "bootstrap_ci_95": [_round(value) for value in list(row["bootstrap_ci_95"])],
                }
                for row in tables["seed-level-statistics"]
            ],
            [
                "metric",
                "direction",
                "method",
                "n",
                "mean",
                "median",
                "sd",
                "mad",
                "bootstrap_ci_95",
            ],
        ),
        "",
        "## Pairwise method contrasts",
        "",
        _markdown_table(
            [
                {
                    "metric": row["metric"],
                    "baseline": METHOD_LABELS.get(
                        str(row["baseline_method"]), str(row["baseline_method"])
                    ),
                    "treatment": METHOD_LABELS.get(
                        str(row["treatment_method"]), str(row["treatment_method"])
                    ),
                    "mean_difference": _round(row["mean_difference"]),
                    "cliffs_delta": _round(row["cliffs_delta"]),
                    "permutation_p": _round(row["permutation_p_value"]),
                    "holm_p": _round(row["holm_adjusted_p_value_within_metric_family"]),
                }
                for row in tables["pairwise-contrasts"]
            ],
            [
                "metric",
                "baseline",
                "treatment",
                "mean_difference",
                "cliffs_delta",
                "permutation_p",
                "holm_p",
            ],
        ),
        "",
        "## Default-control drift",
        "",
        _markdown_table(
            [{key: _round(value) for key, value in row.items()} for row in tables["default-drift"]],
            [
                "seed",
                "valid_controls",
                "fitted_tps_change",
                "absolute_fitted_tps_change_relative_to_control_mean",
                "fitted_p99_change_ms",
                "flagged",
                "campaign_killing",
            ],
        ),
        "",
        "## Safety and failure accounting",
        "",
        _markdown_table(
            tables["safety-and-failures"],
            [
                "method",
                "logical_slots",
                "valid_candidate_observations",
                "completed_but_invalid_slots",
                "candidate_failed_slots",
                "infrastructure_exhausted_slots",
                "retained_infrastructure_attempts",
                "slots_with_infrastructure_retry",
            ],
        ),
        "",
        "## Nondominated candidates",
        "",
        _markdown_table(
            [
                {
                    "seed": row["seed"],
                    "physical_method": row["physical_method"],
                    "global_position": row["global_position"],
                    "throughput_tps": _round(row["throughput_tps"]),
                    "p99_ms": _round(row["p99_ms"]),
                    "requested_configuration": row["requested_configuration"],
                }
                for row in tables["pareto-front"]
            ],
            [
                "seed",
                "physical_method",
                "global_position",
                "throughput_tps",
                "p99_ms",
                "requested_configuration",
            ],
        ),
        "",
        "## Figures",
        "",
    ]
    for name in sorted(FIGURES):
        lines.append(f"- `figures/{name}`")
    lines.extend(
        [
            "",
            "## Inference guards",
            "",
            f"- {analysis['inference_guard']}",
            f"- {analysis['interpretation_guard']}",
            "- Drift flags are transparency markers; they never terminate or discard Wave A.",
            "- Retained infrastructure attempts consume no candidate slot and never train a GP.",
            "- No post-result change to endpoints, the reference point, validity rules, retry "
            "treatment, drift adjustment, or the multiplicity family is permitted without an "
            "append-only deviation record and a separate sensitivity analysis.",
            "",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _write(path: Path, data: bytes, files: list[dict[str, Any]], root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    files.append(
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    )


def render_primary_report(
    analysis: dict[str, Any],
    context: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    """Write the complete Wave A report tree and return its hash index.

    The output is deterministic, UTF-8 without a byte-order mark, and carries a
    separate canonical payload hash and per-file SHA-256 exactly as the frozen
    evidence specification requires.
    """
    root = output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    tables = primary_tables(analysis)
    files: list[dict[str, Any]] = []
    _write(
        root / "primary-report.md",
        render_markdown(analysis, context).encode("utf-8"),
        files,
        root,
    )
    for name in sorted(tables):
        _write(root / "tables" / f"{name}.csv", _csv_bytes(tables[name]), files, root)
    for name in sorted(FIGURES):
        _write(root / "figures" / name, FIGURES[name](analysis).encode("utf-8"), files, root)
    index = {
        "report_kind": "thesis-protocol-v2-primary-wave-a",
        "evidence_role": context.get("evidence_role", "PRIMARY"),
        "deterministic": True,
        "context": context,
        "reference_point": list(PRIMARY_REFERENCE_POINT),
        "table_row_counts": {name: len(rows) for name, rows in sorted(tables.items())},
        "figures": sorted(FIGURES),
        "files": files,
        "payload_sha256_semantics": "canonical JSON hash of the rendered tables, not a file hash",
        "payload_sha256": _canonical_sha256(tables),
    }
    encoded = (json.dumps(index, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
    path = root / "primary-report-index.json"
    path.write_bytes(encoded)
    return {
        "output_dir": str(root),
        "index_path": str(path),
        "index_file_sha256": hashlib.sha256(encoded).hexdigest(),
        "payload_sha256": index["payload_sha256"],
        "file_count": len(files) + 1,
        "total_bytes": sum(int(item["bytes"]) for item in files) + len(encoded),
        "files": files,
    }
