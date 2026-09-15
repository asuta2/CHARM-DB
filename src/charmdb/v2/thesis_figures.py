"""Offline, deterministic final figure selection from authenticated completed evidence.

This is a post-result presentation layer, not a new primary analysis. It never
connects to the target or writes to the source evidence tree.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import statistics
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from charmdb.v2.primary_reporting import (
    FIGURES,
    METHOD_COLORS,
    SEED_COLORS,
    SEED_DASHES,
    figure_default_drift,
)
from charmdb.v2.protocol import PRIMARY_SEARCH_METHODS
from charmdb.v2.secondary_reporting import (
    _adaptive_gain_rows,
    _candidate_contrasts,
    _physical_fronts,
    _quadrant_rows,
)

METHODS = list(PRIMARY_SEARCH_METHODS)
LABELS = dict(zip(METHODS, ["Random", "Sobol", "qLogNEI", "qLogNParEGO", "qLogNEHVI"], strict=True))
SOURCE_PATHS = {
    "final": "primary-wave-b/final-five-seed-analysis.json",
    "initial": "primary-comparison/primary-analysis.json",
    "extension": "primary-wave-b/primary-wave-b-analysis.json",
    "retrospective": "multi-fidelity/phase-a/phase-a-analysis.json",
    "live": "multi-fidelity/phase-b/phase-b-analysis.json",
    "confirmation": "f4-confirmation/f4-analysis.json",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def contained(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Evidence path escapes root: {relative}")
    return path


def load_evidence(root: Path) -> dict[str, Any]:
    """Authenticate every used source, then validate scope and physical accounting."""
    root = root.resolve()
    manifest_bytes = (root / "evidence-sha256-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    entries = {row["relative_path"]: row for row in manifest["files"]}
    verified: dict[str, str] = {}

    def read(relative: str) -> Any:
        data = contained(root, relative).read_bytes()
        actual = digest(data)
        if relative not in entries or actual != entries[relative]["sha256"]:
            raise ValueError(f"Evidence hash mismatch: {relative}")
        verified[relative] = actual
        return json.loads(data)

    exports = {key: read(path) for key, path in SOURCE_PATHS.items()}
    a = exports["final"]["analysis"]
    payload = {k: v for k, v in a.items() if k != "analysis_sha256"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    if digest(encoded) != exports["final"]["analysis_sha256"]:
        raise ValueError("Final analysis payload hash mismatch")
    seeds = sorted(a["analysis_seeds"])
    if len(set(seeds)) != 5 or a["independent_seed_count"] != 5:
        raise ValueError("Exactly five independent primary seeds are required")
    runs = exports["initial"]["history"]["runs"] + exports["extension"]["history"]["runs"]
    if len(runs) != 655 or len({x["primary_run_id"] for x in runs}) != 655:
        raise ValueError("Expected 655 unique physical primary observations")
    contrasts = _candidate_contrasts(runs, seeds)
    quadrants = _quadrant_rows(contrasts, a)
    if len(contrasts) != 750 or len(quadrants) != 25:
        raise ValueError("Incomplete logical candidate contrasts")
    trajectories = a["slot_trajectories"]
    if len(trajectories) != 25:
        raise ValueError("Incomplete trajectories")
    for trajectory in trajectories:
        slots = trajectory["slots"]
        if [x["slot"] for x in slots] != list(range(1, 31)):
            raise ValueError("Expected contiguous 30-slot trajectories")
        if any(y["hypervolume"] < x["hypervolume"] for x, y in pairwise(slots)):
            raise ValueError("Hypervolume must be monotone")
    stages = []
    for seed in seeds:
        for method in METHODS:
            for stage, lo, hi in [("initial", 1, 12), ("adaptive_or_later", 13, 30)]:
                rows = [
                    x
                    for x in contrasts
                    if x["seed"] == seed and x["method"] == method and lo <= x["logical_slot"] <= hi
                ]
                stages.append(
                    {
                        "seed": seed,
                        "method": method,
                        "stage": stage,
                        "n": len(rows),
                        "mean_tps_percent": 100
                        * statistics.mean(x["control_relative_tps"] for x in rows),
                        "mean_p99_delta_ms": statistics.mean(
                            x["control_relative_p99_ms"] for x in rows
                        ),
                    }
                )
    costs = []
    for run in runs:
        relative = f"raw/{run['campaign_id']}/{run['trial_id']}/trial.json"
        trial = read(relative)
        if trial["trial_id"] != run["trial_id"]:
            raise ValueError("Trial identity mismatch")
        for key in ("throughput_tps", "p99_ms"):
            if not math.isclose(trial["objectives"][key], run["objective_values"][key]):
                raise ValueError("Raw trial objectives differ from history")
        life = (
            datetime.fromisoformat(run["trial_completed_at"])
            - datetime.fromisoformat(run["trial_started_at"])
        ).total_seconds()
        restore = float(run["restore_seconds"])
        measure = float(trial["result"]["duration_seconds"])
        other = life - restore - measure
        if not all(math.isfinite(x) and x >= 0 for x in (life, restore, measure, other)):
            raise ValueError("Invalid lifecycle accounting")
        costs.append(
            {
                "primary_run_id": run["primary_run_id"],
                "seed": run["seed"],
                "physical_method": run["method"],
                "lifecycle_seconds": life,
                "restore_seconds": restore,
                "measurement_seconds": measure,
                "other_including_warmup_seconds": other,
            }
        )
    return {
        "analysis": a,
        "seeds": seeds,
        "runs": runs,
        "contrasts": contrasts,
        "quadrants": quadrants,
        "stages": stages,
        "costs": costs,
        "fronts": _physical_fronts(runs, seeds),
        "gains": _adaptive_gain_rows(a),
        "retrospective": exports["retrospective"]["analysis"],
        "live": exports["live"]["analysis"],
        "confirmation": exports["confirmation"]["analysis"],
        "verified_sources": verified,
        "manifest_sha256": digest(manifest_bytes),
    }


def text(x: float, y: float, value: object, size: int = 13, anchor: str = "start") -> str:
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size}" text-anchor="{anchor}">'
        f"{html.escape(str(value))}</text>"
    )


def line(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    color: str = "#cbd0d5",
    width: float = 1,
    dash: str = "",
) -> str:
    return (
        f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
        f'stroke="{color}" stroke-width="{width}" stroke-dasharray="{dash}"/>'
    )


def marker(x: float, y: float, color: str, index: int = 0, radius: float = 4) -> str:
    title = f"<title>{html.escape(color)}</title>"
    if index == 0:
        return f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{radius}" fill="{color}">{title}</circle>'
    shapes: dict[int, list[tuple[float, float]]] = {
        1: [(-1, -1), (1, -1), (1, 1), (-1, 1)],
        2: [(0, -1.4), (1.3, 1), (-1.3, 1)],
        3: [(0, -1.4), (1.2, 0), (0, 1.4), (-1.2, 0)],
        4: [(-1.3, -1), (1.3, -1), (0, 1.4)],
    }
    vertices = shapes[index]
    points = " ".join(f"{x + dx * radius:.2f},{y + dy * radius:.2f}" for dx, dy in vertices)
    return f'<polygon points="{points}" fill="{color}">{title}</polygon>'


class Panel:
    def __init__(
        self,
        body: list[str],
        left: float,
        top: float,
        width: float,
        height: float,
        title: str,
        xlim: tuple[float, float],
        ylim: tuple[float, float],
        xlabel: str,
        ylabel: str,
        categorical: bool = False,
    ) -> None:
        self.body, self.left, self.top = body, left, top
        self.width, self.height, self.xlim, self.ylim = width, height, xlim, ylim
        body.append(text(left, top - 22, title, 16))
        for i in range(5):
            value = ylim[0] + (ylim[1] - ylim[0]) * i / 4
            y = self.y(value)
            body.extend(
                [
                    line(left, y, left + width, y, "#e5e7eb"),
                    text(
                        left - 9,
                        y + 4,
                        f"{value:,.2f}" if abs(value) < 10 else f"{value:,.1f}",
                        11,
                        "end",
                    ),
                ]
            )
        body.append(line(left, top, left, top + height))
        if not categorical:
            for i in range(5):
                value = xlim[0] + (xlim[1] - xlim[0]) * i / 4
                body.append(text(self.x(value), top + height + 20, f"{value:,.0f}", 11, "middle"))
        body.append(text(left + width / 2, top + height + 49, xlabel, 13, "middle"))
        cx, cy = left - 65, top + height / 2
        body.append(
            f'<g transform="rotate(-90 {cx} {cy})">{text(cx, cy, ylabel, 12, "middle")}</g>'
        )
        if ylim[0] < 0 < ylim[1]:
            body.append(line(left, self.y(0), left + width, self.y(0), "#6b7280", 1, "4 3"))

    def x(self, value: float) -> float:
        return self.left + self.width * (value - self.xlim[0]) / (self.xlim[1] - self.xlim[0])

    def y(self, value: float) -> float:
        return self.top + self.height * (1 - (value - self.ylim[0]) / (self.ylim[1] - self.ylim[0]))

    def methods(self) -> None:
        for i, method in enumerate(METHODS):
            self.body.append(
                text(self.x(i), self.top + self.height + 22, LABELS[method], 11, "middle")
            )

    def path(
        self, points: list[tuple[float, float]], color: str, width: float = 1.5, dash: str = ""
    ) -> None:
        coords = " ".join(f"{self.x(x):.2f},{self.y(y):.2f}" for x, y in points)
        self.body.append(
            f'<polyline points="{coords}" fill="none" stroke="{color}" '
            f'stroke-width="{width}" stroke-dasharray="{dash}"/>'
        )


def bounds(values: list[float], zero: bool = False) -> tuple[float, float]:
    values = values + ([0] if zero else [])
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.12 or 1
    return lo - pad, hi + pad


def document(title: str, body: list[str], notes: list[str], height: int = 880) -> str:
    header = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" '
        f'height="{height}" viewBox="0 0 1200 {height}" role="img">',
        f"<title>{html.escape(title)}</title>",
        "<style>text{font-family:Arial,sans-serif;fill:#202630}</style>",
        '<rect width="100%" height="100%" fill="white"/>',
        text(40, 32, title, 22),
    ]
    footer = [text(40, height - 18 * (len(notes) - i), note, 12) for i, note in enumerate(notes)]
    return "\n".join(header + body + footer + ["</svg>"]) + "\n"


def seed_legend(body: list[str], seeds: list[int], y: float = 64) -> None:
    for i, seed in enumerate(seeds):
        x = 45 + i * 222
        body.extend(
            [
                marker(x, y - 4, SEED_COLORS[i], i),
                text(x + 12, y, f"Seed {seed}", 12),
                line(x + 145, y - 4, x + 187, y - 4, SEED_COLORS[i], 1.5, SEED_DASHES[i]),
            ]
        )


def quality_figure(e: dict[str, Any]) -> str:
    a, seeds = e["analysis"], e["seeds"]
    body: list[str] = []
    seed_legend(body, seeds)
    specifications = [
        ("A  Typical throughput", "mean_control_relative_tps", 100, "TPS change (%)", True),
        ("B  Typical tail latency", "mean_control_relative_p99_ms", 1, "p99 difference (ms)", True),
        ("C  Candidates improving both objectives", "domination", 100 / 30, "Share (%)", True),
        ("D  Best observed throughput", "best_throughput_tps", 1, "TPS", False),
    ]
    for j, (title, key, scale, ylabel, zero) in enumerate(specifications):
        field = "observations_dominating_local_control" if key == "domination" else key
        values = [x[field] * scale for x in a["seed_method_results"]]
        lim = (0.0, 100.0) if key == "domination" else bounds(values, zero)
        p = Panel(
            body,
            105 + (j % 2) * 590,
            130 + (j // 2) * 330,
            455,
            215,
            title,
            (-0.5, 4.5),
            lim,
            "",
            ylabel,
            True,
        )
        p.methods()
        for i, method in enumerate(METHODS):
            rows = [x for x in a["seed_method_results"] if x["method"] == method]
            mean = statistics.mean(x[field] * scale for x in rows)
            body.append(line(p.x(i) - 17, p.y(mean), p.x(i) + 17, p.y(mean), "#111111", 3))
            for row in rows:
                s = seeds.index(row["seed"])
                body.append(
                    marker(p.x(i + (s - 2) * 0.055), p.y(row[field] * scale), SEED_COLORS[s], s)
                )
    return document(
        "Candidate quality is repeatable; throughput extremes separate less",
        body,
        [
            "Five seeds; 30 logical slots per method/seed. Symbols identify "
            "seeds; black bars are means.",
            "TPS and p99 contrasts use interpolated same-seed controls. Candidate"
            " shares are descriptive, not independent replicates.",
            "All points are shown instead of distribution smoothing. Registered "
            "seed-bootstrap intervals remain in the accompanying table.",
        ],
        850,
    )


def adaptation_figure(e: dict[str, Any]) -> str:
    body: list[str] = []
    seeds = e["seeds"]
    for i, method in enumerate(METHODS):
        x = 45 + i * 220
        body.extend(
            [
                line(x, 64, x + 25, 64, METHOD_COLORS[method], 3),
                text(x + 32, 68, LABELS[method], 12),
            ]
        )
    ts = e["analysis"]["slot_trajectories"]
    p = Panel(
        body,
        100,
        125,
        1000,
        230,
        "A  Hypervolume over the full logical budget",
        (1, 30),
        bounds([s["hypervolume"] for t in ts for s in t["slots"]]),
        "Logical candidate slot",
        "Hypervolume (reference: TPS 0, p99 40 ms)",
    )
    for t in ts:
        p.path(
            [(s["slot"], s["hypervolume"]) for s in t["slots"]],
            METHOD_COLORS[t["method"]],
            0.9,
            SEED_DASHES[seeds.index(t["seed"])],
        )
    for method in METHODS:
        rows = [t for t in ts if t["method"] == method]
        p.path(
            [
                (slot, statistics.mean(t["slots"][slot - 1]["hypervolume"] for t in rows))
                for slot in range(1, 31)
            ],
            METHOD_COLORS[method],
            3,
        )
    body.extend(
        [
            line(p.x(12), p.top, p.x(12), p.top + p.height, "#333333", 1.5, "5 3"),
            text(p.x(12) + 8, p.top + 18, "End of initialization", 12),
        ]
    )
    for j, (key, title, ylabel) in enumerate(
        [
            (
                "mean_tps_percent",
                "B  Typical throughput: initial to later slots",
                "TPS change vs local default (%)",
            ),
            (
                "mean_p99_delta_ms",
                "C  Typical p99: initial to later slots",
                "p99 difference vs local default (ms)",
            ),
        ]
    ):
        p = Panel(
            body,
            100 + j * 590,
            480,
            455,
            235,
            title,
            (-0.5, 4.5),
            bounds([x[key] for x in e["stages"]], True),
            "",
            ylabel,
            True,
        )
        p.methods()
        for i, method in enumerate(METHODS):
            for s, seed in enumerate(seeds):
                rows = [x for x in e["stages"] if x["seed"] == seed and x["method"] == method]
                x1, x2 = i - 0.22 + s * 0.025, i + 0.10 + s * 0.025
                p.path([(x1, rows[0][key]), (x2, rows[1][key])], SEED_COLORS[s], 1.3)
                body.append(marker(p.x(x1), p.y(rows[0][key]), SEED_COLORS[s], s, 3))
                body.append(marker(p.x(x2), p.y(rows[1][key]), SEED_COLORS[s], s, 4.5))
    seed_legend(body, seeds, 786)
    return document(
        "Initialization quality and the contribution of later search",
        body,
        [
            "A: thin lines are seeds; thick lines are method means. The fixed "
            "hypervolume reference and all 30 slots are retained.",
            "B/C: each segment joins slots 1-12 (left) to 13-30 (right). BO "
            "shares its initialization; random/Sobol use their own first 12.",
            "Stage contrasts are descriptive and chronologically confounded. "
            "Controls are interpolated; no causal stage effect is claimed.",
        ],
        870,
    )


def fidelity_figure(e: dict[str, Any]) -> str:
    body: list[str] = []
    a, live = e["retrospective"], e["live"]
    summaries = [x for x in a["summaries"] if isinstance(x["seed"], int)]
    p = Panel(
        body,
        100,
        140,
        440,
        240,
        "A  Retrospective ordering: three seeds",
        (40, 140),
        (0.75, 1.0),
        "Prefix duration (seconds)",
        "Spearman correlation of relative TPS",
        categorical=True,
    )
    for window in (60, 120):
        body.append(text(p.x(window), p.top + p.height + 20, window, 12, "middle"))
    for seed in sorted({x["seed"] for x in summaries}):
        rows = sorted(
            [x for x in summaries if x["seed"] == seed], key=lambda x: x["window_seconds"]
        )
        s = e["seeds"].index(seed)
        p.path(
            [(x["window_seconds"], x["spearman_control_relative_tps"]) for x in rows],
            SEED_COLORS[s],
            2,
        )
        for row in rows:
            body.append(
                marker(
                    p.x(row["window_seconds"]),
                    p.y(row["spearman_control_relative_tps"]),
                    SEED_COLORS[s],
                    s,
                )
            )
        body.extend(
            [
                marker(
                    100 + sorted({x["seed"] for x in summaries}).index(seed) * 160,
                    460,
                    SEED_COLORS[s],
                    s,
                ),
                text(112 + sorted({x["seed"] for x in summaries}).index(seed) * 160, 465, seed, 11),
            ]
        )
    body.append(text(650, 118, "B  Conservative promotion", 16))
    groups = [
        (
            "Retrospective 60 s",
            next(
                x
                for x in a["summaries"]
                if x["seed"] == "pooled-descriptive" and x["window_seconds"] == 60
            ),
        ),
        (
            "Retrospective 120 s",
            next(
                x
                for x in a["summaries"]
                if x["seed"] == "pooled-descriptive" and x["window_seconds"] == 120
            ),
        ),
        ("Live demonstration", live["counts"]),
    ]
    for i, (label, row) in enumerate(groups):
        y, fraction = 170 + i * 95, row["promoted"] / row["candidates"]
        body.append(text(650, y - 16, label))
        for x, width, color in [
            (650, 430 * fraction, "#0072B2"),
            (650 + 430 * fraction, 430 * (1 - fraction), "#D55E00"),
        ]:
            body.append(f'<rect x="{x}" y="{y}" width="{width}" height="22" fill="{color}"/>')
        body.append(
            text(
                650,
                y + 43,
                f"{row['promoted']}/{row['candidates']} promoted; {row['rejected']} rejected",
                12,
            )
        )
    body.append(text(650, 460, "Blue: promoted   Orange: rejected", 12))
    body.append(text(100, 535, "C  Primary evaluation lifecycle limits savings", 16))
    total = sum(x["lifecycle_seconds"] for x in e["costs"])
    cursor = 100.0
    for key, label, color in [
        ("restore_seconds", "Restore", "#332288"),
        ("measurement_seconds", "Measurement", "#0072B2"),
        ("other_including_warmup_seconds", "Warm-up + other", "#D55E00"),
    ]:
        seconds = sum(x[key] for x in e["costs"])
        width = seconds / total * 1000
        body.append(f'<rect x="{cursor}" y="560" width="{width}" height="34" fill="{color}"/>')
        body.append(text(cursor + width / 2, 620, f"{label}: {seconds / total:.1%}", 13, "middle"))
        cursor += width
    body.append(
        text(
            100,
            664,
            f"Live savings: {live['time']['saved_seconds']:.0f} s / "
            f"{live['time']['saved_percent_of_counterfactual_total']:.3f}% "
            "of matched all-F3 lifecycle",
            18,
        )
    )
    body.append(
        text(
            100,
            695,
            "The matched all-F3 denominator is recorded accounting, not an executed comparison.",
            13,
        )
    )
    return document(
        "Predictive prefixes do not imply large lifecycle savings",
        body,
        [
            "Retrospective: 378 physical candidates, three seeds; zero top-"
            "quartile false rejections at the registered 0.8 TPS floor.",
            "Live: one seed, fixed Sobol design, 30 candidates + five controls. "
            "The rejected candidate has no observed F3 outcome.",
            "Lifecycle: 655 successful primary trials, each counted once. Other "
            "includes configured 600 s warm-up; failed attempts/idle gaps "
            "excluded.",
            "Promotion counts are descriptive. Retrospective and live scopes are "
            "separate; neither is a five-seed fidelity experiment.",
        ],
        825,
    )


def f4_deltas(e: dict[str, Any]) -> list[dict[str, Any]]:
    observations = e["confirmation"]["observations"]
    controls = {x["repetition_block"]: x for x in observations if x["treatment"] == "DEFAULT"}
    if len(observations) != 20 or len(controls) != 4:
        raise ValueError("F4 requires 20 observations in four matched blocks")
    expected = {
        (block, treatment) for block in range(1, 5) for treatment in ("DEFAULT", "T", "L", "H", "E")
    }
    if {(x["repetition_block"], x["treatment"]) for x in observations} != expected:
        raise ValueError("F4 requires each treatment exactly once in each matched block")
    result = []
    for x in observations:
        if x["treatment"] == "DEFAULT":
            continue
        c = controls[x["repetition_block"]]
        result.append(
            {
                "treatment": x["treatment"],
                "block": x["repetition_block"],
                "tps_percent": 100 * (x["throughput_tps"] / c["throughput_tps"] - 1),
                "p99_delta_ms": x["p99_ms"] - c["p99_ms"],
            }
        )
    return result


def confirmation_figure(e: dict[str, Any]) -> str:
    body: list[str] = []
    rows = f4_deltas(e)
    for i in range(4):
        body.extend(
            [
                marker(75 + 240 * i, 70, SEED_COLORS[i], i),
                text(90 + 240 * i, 74, f"Confirmation block {i + 1}"),
            ]
        )
    for j, (key, title, ylabel, gate) in enumerate(
        [
            ("tps_percent", "A  Throughput versus matched default", "TPS difference (%)", -5),
            ("p99_delta_ms", "B  Tail latency versus matched default", "p99 difference (ms)", -1),
        ]
    ):
        p = Panel(
            body,
            100 + j * 590,
            145,
            455,
            290,
            title,
            (-0.5, 3.5),
            bounds([x[key] for x in rows] + [gate], True),
            "Finalist configuration",
            ylabel,
            True,
        )
        body.append(line(p.left, p.y(gate), p.left + p.width, p.y(gate), "#555555", 1.5, "6 3"))
        for i, treatment in enumerate(["T", "L", "H", "E"]):
            values = [x for x in rows if x["treatment"] == treatment]
            mean = statistics.mean(x[key] for x in values)
            body.append(line(p.x(i) - 17, p.y(mean), p.x(i) + 17, p.y(mean), "#111111", 3))
            body.append(
                text(
                    p.x(i),
                    457,
                    treatment
                    + (
                        " (selected)"
                        if treatment == e["confirmation"]["selected_treatment"]
                        else ""
                    ),
                    12,
                    "middle",
                )
            )
            for row in values:
                b = row["block"] - 1
                body.append(marker(p.x(i + (b - 1.5) * 0.06), p.y(row[key]), SEED_COLORS[b], b, 5))
        body.append(
            text(
                p.left,
                510,
                f"Dashed line: frozen mean gate ({gate:+} {'%' if j == 0 else 'ms'})",
                12,
            )
        )
    return document(
        "Finalist confirmation: repeated gains and a latency-first choice",
        body,
        [
            "Four matched blocks, not five primary seeds. Black bars are means; "
            "all 16 finalist observations are shown.",
            "E was selected by lowest mean p99 among eligible finalists; it is "
            "not the throughput winner. All finalists passed all frozen gates.",
            "Other gates require valid repetitions and mean TPS within 95% of the"
            " highest finalist mean. No optimizer-family superiority is inferred.",
        ],
        620,
    )


def pareto_figure(e: dict[str, Any]) -> str:
    body: list[str] = []
    for i, method in enumerate(METHODS):
        x = 40.0 + i * 220
        body.extend([marker(x, 65, METHOD_COLORS[method]), text(x + 12, 69, LABELS[method], 12)])
    xlim = bounds([x["throughput_tps"] for x in e["fronts"]])
    ylim = bounds([x["p99_ms"] for x in e["fronts"]])
    for j, seed in enumerate(e["seeds"]):
        front = [x for x in e["fronts"] if x["seed"] == seed]
        p = Panel(
            body,
            95 + (j % 2) * 590,
            135 + (j // 2) * 330,
            460,
            210,
            f"Seed {seed}: {len(front)} frontier points",
            xlim,
            ylim,
            "Throughput (TPS)",
            "p99 latency (ms), lower is better",
        )
        p.path([(x["throughput_tps"], x["p99_ms"]) for x in front], "#9ca3af")
        occupied: list[tuple[float, float, float, float]] = []
        for i, row in enumerate(front):
            color = METHOD_COLORS.get(row["physical_method"], "#333333")
            x, y = p.x(row["throughput_tps"]), p.y(row["p99_ms"])
            body.append(
                marker(x, y, color, 3 if row["physical_method"] == "bo_shared_initial" else 0, 5)
            )
            for dx, dy in [(7, -10), (7, 20), (-37, -10), (-37, 20), (7, -30), (7, 40), (-37, -30)]:
                box = (x + dx, y + dy - 12, x + dx + 34, y + dy + 3)
                if not any(
                    box[0] < b[2] and box[2] > b[0] and box[1] < b[3] and box[3] > b[1]
                    for b in occupied
                ):
                    break
            occupied.append(box)
            body.append(line(x, y, x + dx, y + dy - 4, "#777777", 0.6))
            body.append(text(x + dx, y + dy, f"P{j + 1}.{i + 1}", 11))
    body.extend(
        [
            text(700, 855, "Appendix: physical frontiers, separated by seed", 16),
            text(700, 886, "Shared initialization: one dark diamond, measured once.", 12),
            text(700, 912, "Point IDs link to the configuration/objective CSV.", 12),
            text(700, 938, "Common axes; fronts are not pooled across seeds.", 12),
            text(700, 964, "Segments show observed alternatives, not an interpolated model.", 12),
        ]
    )
    return document(
        "Per-seed Pareto alternatives and changing frontier ownership",
        body,
        [
            "Frontier membership uses raw TPS/p99. It does not remove within-seed"
            " drift or establish statistical superiority.",
            "No knee or composite winner is selected. Full normalized "
            "configurations and local-control contrasts are in pareto-"
            "configurations.csv.",
        ],
        1120,
    )


def csv_data(rows: list[dict[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    fields = list(dict.fromkeys(k for row in rows for k in row))
    writer = csv.DictWriter(stream, fields)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v
                for k, v in row.items()
            }
        )
    return stream.getvalue()


def render(root: Path, output: Path) -> dict[str, Any]:
    root, output = root.resolve(), output.resolve()
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError("Output must be separate from the immutable evidence tree")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use a new or empty output directory")
    e = load_evidence(root)
    figures = {
        "01-candidate-quality.svg": quality_figure(e),
        "02-initialization-and-adaptation.svg": adaptation_figure(e),
        "03-default-drift.svg": figure_default_drift(e["analysis"]),
        "04-fidelity-and-lifecycle.svg": fidelity_figure(e),
        "05-finalist-confirmation.svg": confirmation_figure(e),
        "06-per-seed-pareto.svg": pareto_figure(e),
    }
    fronts = []
    for j, seed in enumerate(e["seeds"]):
        for i, row in enumerate(x for x in e["fronts"] if x["seed"] == seed):
            fronts.append({"point_id": f"P{j + 1}.{i + 1}", **row})
    tables = {
        "seed-method-outcomes": e["analysis"]["seed_method_results"],
        "seed-level-statistics": e["analysis"]["seed_level_statistics"],
        "candidate-contrasts": e["contrasts"],
        "stage-quality": e["stages"],
        "hypervolume-gains": e["gains"],
        "pareto-configurations": fronts,
        "physical-lifecycle": e["costs"],
        "f4-matched-deltas": f4_deltas(e),
        "fidelity-summaries": e["retrospective"]["summaries"],
    }
    output.mkdir(parents=True, exist_ok=True)
    files = {}

    def write(relative: str, content: str) -> None:
        dest = contained(output, relative)
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        dest.write_bytes(data)
        files[relative] = {"sha256": digest(data), "bytes": len(data)}

    for name, svg in figures.items():
        write(f"figures/{name}", svg)
    for name, rows in tables.items():
        write(f"tables/{name}.csv", csv_data(rows))
    # Refreshed copies of every scientifically usable original primary figure.
    # The known erroneous safety chart is deliberately replaced by the erratum.
    for name, renderer in FIGURES.items():
        if "safety" not in name:
            write(f"existing-primary/{name}", renderer(e["analysis"]))
    write(
        "README.md",
        "# Final thesis figures\n\n"
        "Post-result presentation from authenticated completed artifacts. Five primary seeds; "
        "fidelity and confirmation retain their separate scopes. No new endpoint or experiment.\n\n"
        "Figures 01-03: Results. Figure 04: Discussion. Figure 05: F4 Results. "
        "Figure 06: Appendix. All SVGs are standalone vector artifacts.\n\n"
        "Seed colors and symbols are consistent across primary figures; method colors identify "
        "methods on trajectories/Pareto plots. Dark mean bars and seed traces are intentional "
        "summaries, not additional categories. F4 colors identify confirmation blocks.\n\n"
        "existing-primary/ contains refreshed versions of the five usable original plots. "
        "Archived evidence and submission packages are unchanged. The old safety figure is "
        "not suitable for publication: 655 physical slots, 657 attempts, two failed attempts "
        "and two retried slots; shared logical attributions must not be summed.\n\n"
        "All lifecycle rows count successful physical evaluations once, excluding failed attempts, "
        "idle time and optimizer computation. Stage comparisons are descriptive. "
        "Bootstrap intervals "
        "are retained in tables; seed points, not candidate-based error bars, are plotted.\n",
    )
    cards = "\n".join(
        f'<section><h2>{html.escape(name)}</h2><img src="figures/{name}" '
        'style="width:100%;height:auto"/></section>'
        for name in figures
    )
    write(
        "index.html",
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        "<title>Final thesis figures</title><style>body{max-width:1200px;margin:30px auto;"
        "font-family:Arial;background:#eee}section{background:white;margin:30px 0;padding:20px}"
        "</style><h1>Final thesis figure set</h1>" + cards + "</html>",
    )
    index = {
        "schema_version": 1,
        "presentation_revision": "selective-five-seed-v1",
        "renderer_sha256": digest(Path(__file__).read_bytes()),
        "primary_seed_count": 5,
        "physical_observations": 655,
        "source_manifest_sha256": e["manifest_sha256"],
        "verified_source_files": e["verified_sources"],
        "files": files,
        "seed_colors": dict(zip(map(str, e["seeds"]), SEED_COLORS, strict=True)),
        "method_colors": METHOD_COLORS,
        "table_rows": {k: len(v) for k, v in tables.items()},
    }
    (output / "figure-index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return index
