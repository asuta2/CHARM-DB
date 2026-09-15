# ruff: noqa: E501

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS = ROOT / "artifacts"
OUTPUT_DIR = ARTIFACTS / "reports" / "scale10-sanity-seed20260731"
BASE_SEED = 20260731
P99_SLO_MS = 20.0

METHODS = {
    "random": {
        "label": "Random search",
        "campaign_id": "ba3c121c-fb07-57f0-8e28-7cf7b24915b1",
        "color": "#D97706",
    },
    "standard_bo": {
        "label": "Unconstrained BO (qLogNEHVI)",
        "campaign_id": "3e40e068-12f4-5fd9-bc64-8db797f59d03",
        "color": "#2563EB",
    },
}


@dataclass(frozen=True)
class Trial:
    method: str
    method_label: str
    campaign_id: str
    iteration: int
    trial_id: str
    stage: str
    feasible: bool
    throughput_tps: float
    p99_ms: float
    failures: int
    configuration: dict[str, str]
    source_path: Path
    source_sha256: str


@dataclass(frozen=True)
class Incumbent:
    method: str
    method_label: str
    iteration: int
    best_throughput_tps: float | None
    incumbent_p99_ms: float | None
    incumbent_trial_id: str | None
    improved: bool


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_trials() -> list[Trial]:
    trials: list[Trial] = []
    for method, definition in METHODS.items():
        campaign_id = str(definition["campaign_id"])
        campaign_dir = ARTIFACTS / "raw" / campaign_id
        if not campaign_dir.is_dir():
            raise FileNotFoundError(f"campaign artifact directory is missing: {campaign_dir}")
        for source_path in campaign_dir.glob("*/trial.json"):
            payload: dict[str, Any] = json.loads(source_path.read_text(encoding="utf-8"))
            workflow = dict(payload["context"]["workflow_payload"])
            result = dict(payload["result"])
            constraints = dict(payload["constraints"])
            configuration = {
                str(name): str(value)
                for name, value in dict(payload["active_configuration"]).items()
            }
            iteration = int(workflow["seed"]) - BASE_SEED + 1
            if iteration < 1:
                raise ValueError(f"invalid derived iteration for {source_path}")
            if method == "standard_bo":
                stage = "sobol_initialization" if iteration <= 4 else "unconstrained_qlognehvi"
            else:
                stage = "random"
            trials.append(
                Trial(
                    method=method,
                    method_label=str(definition["label"]),
                    campaign_id=campaign_id,
                    iteration=iteration,
                    trial_id=str(payload["trial_id"]),
                    stage=stage,
                    feasible=bool(constraints["feasible"]),
                    throughput_tps=float(result["throughput_tps"]),
                    p99_ms=float(result["p99_ms"]),
                    failures=int(result["failures"]),
                    configuration=configuration,
                    source_path=source_path,
                    source_sha256=sha256(source_path),
                )
            )
    trials.sort(key=lambda item: (item.method, item.iteration))
    expected = {"random": 20, "standard_bo": 19}
    for method, count in expected.items():
        method_trials = [trial for trial in trials if trial.method == method]
        if len(method_trials) != count:
            raise ValueError(f"{method} has {len(method_trials)} trials; expected {count}")
        if [trial.iteration for trial in method_trials] != list(range(1, count + 1)):
            raise ValueError(f"{method} iterations are not contiguous from one")
    return trials


def incumbents(trials: list[Trial]) -> list[Incumbent]:
    rows: list[Incumbent] = []
    for method in METHODS:
        current: Trial | None = None
        for trial in (item for item in trials if item.method == method):
            improved = trial.feasible and (
                current is None or trial.throughput_tps > current.throughput_tps
            )
            if improved:
                current = trial
            rows.append(
                Incumbent(
                    method=method,
                    method_label=trial.method_label,
                    iteration=trial.iteration,
                    best_throughput_tps=(current.throughput_tps if current is not None else None),
                    incumbent_p99_ms=current.p99_ms if current is not None else None,
                    incumbent_trial_id=current.trial_id if current is not None else None,
                    improved=improved,
                )
            )
    return rows


def write_raw_csv(trials: list[Trial], path: Path) -> None:
    fields = [
        "method",
        "method_label",
        "campaign_id",
        "iteration",
        "trial_id",
        "stage",
        "feasible",
        "throughput_tps",
        "p99_ms",
        "failures",
        "random_page_cost",
        "work_mem_kb",
        "effective_io_concurrency",
        "source_relative_path",
        "source_sha256",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for trial in trials:
            writer.writerow(
                {
                    "method": trial.method,
                    "method_label": trial.method_label,
                    "campaign_id": trial.campaign_id,
                    "iteration": trial.iteration,
                    "trial_id": trial.trial_id,
                    "stage": trial.stage,
                    "feasible": str(trial.feasible).lower(),
                    "throughput_tps": f"{trial.throughput_tps:.6f}",
                    "p99_ms": f"{trial.p99_ms:.6f}",
                    "failures": trial.failures,
                    "random_page_cost": trial.configuration["random_page_cost"],
                    "work_mem_kb": trial.configuration["work_mem"],
                    "effective_io_concurrency": trial.configuration["effective_io_concurrency"],
                    "source_relative_path": str(trial.source_path.relative_to(ROOT)).replace(
                        "\\", "/"
                    ),
                    "source_sha256": trial.source_sha256,
                }
            )


def write_incumbent_csv(rows: list[Incumbent], path: Path) -> None:
    fields = [
        "method",
        "method_label",
        "iteration",
        "best_feasible_throughput_tps",
        "incumbent_p99_ms",
        "incumbent_trial_id",
        "improved_at_iteration",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "method": row.method,
                    "method_label": row.method_label,
                    "iteration": row.iteration,
                    "best_feasible_throughput_tps": (
                        "" if row.best_throughput_tps is None else f"{row.best_throughput_tps:.6f}"
                    ),
                    "incumbent_p99_ms": (
                        "" if row.incumbent_p99_ms is None else f"{row.incumbent_p99_ms:.6f}"
                    ),
                    "incumbent_trial_id": row.incumbent_trial_id or "",
                    "improved_at_iteration": str(row.improved).lower(),
                }
            )


def x_position(iteration: int) -> float:
    return 105.0 + (iteration - 1) * (1015.0 / 19.0)


def y_position(value: float, minimum: float, maximum: float, top: float, height: float) -> float:
    return top + height - ((value - minimum) / (maximum - minimum)) * height


def step_path(
    rows: list[Incumbent],
    attribute: str,
    minimum: float,
    maximum: float,
    top: float,
    height: float,
) -> str:
    points: list[tuple[float, float]] = []
    for row in rows:
        value = getattr(row, attribute)
        if value is not None:
            points.append(
                (x_position(row.iteration), y_position(value, minimum, maximum, top, height))
            )
    if not points:
        return ""
    commands = [f"M {points[0][0]:.2f} {points[0][1]:.2f}"]
    for x_value, y_value in points[1:]:
        commands.append(f"H {x_value:.2f}")
        commands.append(f"V {y_value:.2f}")
    return " ".join(commands)


def write_svg(rows: list[Incumbent], trials: list[Trial], path: Path) -> None:
    width = 1280
    height = 850
    plot_left = 105
    plot_right = 1120
    throughput_top = 150
    plot_height = 245
    p99_top = 500
    throughput_min, throughput_max = 390.0, 460.0
    p99_min, p99_max = 16.0, 20.0
    elements: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        "<style>",
        "text{font-family:Inter,Segoe UI,Arial,sans-serif;fill:#172033}",
        ".title{font-size:26px;font-weight:700}.subtitle{font-size:14px;fill:#536076}",
        ".axis{font-size:12px;fill:#5B6578}.label{font-size:14px;font-weight:600}",
        ".summary{font-size:13px}.grid{stroke:#D9DEE8;stroke-width:1}",
        ".axisline{stroke:#7A8496;stroke-width:1.2}.series{fill:none;stroke-width:3.5}",
        ".marker{stroke:#FFFFFF;stroke-width:2}.slo{stroke:#B42318;stroke-width:1.5;stroke-dasharray:7 5}",
        "</style>",
        '<rect width="1280" height="850" fill="#F8FAFC"/>',
        '<rect x="40" y="30" width="1200" height="790" rx="16" fill="#FFFFFF" stroke="#E3E7EF"/>',
        '<text x="70" y="75" class="title">Scale-10 sanity check: best feasible result by iteration</text>',
        '<text x="70" y="102" class="subtitle">Seed 20260731 · 3 knobs · 120 s warm-up + 600 s measurement · p99 SLO ≤ 20 ms</text>',
        f'<text x="{plot_left}" y="135" class="label">Best feasible throughput (TPS)</text>',
        f'<text x="{plot_left}" y="485" class="label">p99 of the best-throughput feasible incumbent (ms)</text>',
    ]

    for value in range(390, 461, 10):
        y_value = y_position(
            float(value), throughput_min, throughput_max, throughput_top, plot_height
        )
        elements.extend(
            [
                f'<line x1="{plot_left}" y1="{y_value:.2f}" x2="{plot_right}" y2="{y_value:.2f}" class="grid"/>',
                f'<text x="{plot_left - 12}" y="{y_value + 4:.2f}" text-anchor="end" class="axis">{value}</text>',
            ]
        )
    for value in range(16, 21):
        y_value = y_position(float(value), p99_min, p99_max, p99_top, plot_height)
        line_class = "slo" if value == 20 else "grid"
        elements.extend(
            [
                f'<line x1="{plot_left}" y1="{y_value:.2f}" x2="{plot_right}" y2="{y_value:.2f}" class="{line_class}"/>',
                f'<text x="{plot_left - 12}" y="{y_value + 4:.2f}" text-anchor="end" class="axis">{value}</text>',
            ]
        )
    elements.append(
        f'<text x="{plot_right - 5}" y="{p99_top + 16}" text-anchor="end" '
        'style="font-size:12px;fill:#B42318">20 ms SLO</text>'
    )

    for iteration in range(1, 21):
        x_value = x_position(iteration)
        if iteration in {1, 5, 10, 15, 20}:
            elements.extend(
                [
                    f'<line x1="{x_value:.2f}" y1="{throughput_top}" x2="{x_value:.2f}" '
                    f'y2="{throughput_top + plot_height}" class="grid"/>',
                    f'<line x1="{x_value:.2f}" y1="{p99_top}" x2="{x_value:.2f}" '
                    f'y2="{p99_top + plot_height}" class="grid"/>',
                    f'<text x="{x_value:.2f}" y="{p99_top + plot_height + 25}" '
                    f'text-anchor="middle" class="axis">{iteration}</text>',
                ]
            )
    elements.append(
        f'<text x="{(plot_left + plot_right) / 2:.2f}" y="{p99_top + plot_height + 53}" '
        'text-anchor="middle" class="axis">Iteration</text>'
    )

    for method, definition in METHODS.items():
        method_rows = [row for row in rows if row.method == method]
        color = str(definition["color"])
        throughput_path = step_path(
            method_rows,
            "best_throughput_tps",
            throughput_min,
            throughput_max,
            throughput_top,
            plot_height,
        )
        p99_path = step_path(
            method_rows, "incumbent_p99_ms", p99_min, p99_max, p99_top, plot_height
        )
        elements.extend(
            [
                f'<path d="{throughput_path}" class="series" stroke="{color}"/>',
                f'<path d="{p99_path}" class="series" stroke="{color}"/>',
            ]
        )
        for row in method_rows:
            if not row.improved:
                continue
            throughput_y = y_position(
                float(row.best_throughput_tps),
                throughput_min,
                throughput_max,
                throughput_top,
                plot_height,
            )
            p99_y = y_position(float(row.incumbent_p99_ms), p99_min, p99_max, p99_top, plot_height)
            elements.extend(
                [
                    f'<circle cx="{x_position(row.iteration):.2f}" cy="{throughput_y:.2f}" '
                    f'r="5" fill="{color}" class="marker"/>',
                    f'<circle cx="{x_position(row.iteration):.2f}" cy="{p99_y:.2f}" '
                    f'r="5" fill="{color}" class="marker"/>',
                ]
            )

    random_trial = max(
        (trial for trial in trials if trial.method == "random" and trial.feasible),
        key=lambda item: item.throughput_tps,
    )
    bo_trial = max(
        (trial for trial in trials if trial.method == "standard_bo" and trial.feasible),
        key=lambda item: item.throughput_tps,
    )
    elements.extend(
        [
            '<line x1="760" y1="123" x2="790" y2="123" stroke="#D97706" stroke-width="4"/>',
            '<text x="800" y="128" class="summary">Random search</text>',
            '<line x1="930" y1="123" x2="960" y2="123" stroke="#2563EB" stroke-width="4"/>',
            '<text x="970" y="128" class="summary">Unconstrained BO</text>',
            '<rect x="845" y="170" width="255" height="102" rx="10" fill="#EFF6FF" stroke="#BFDBFE"/>',
            f'<text x="865" y="198" class="summary">Final BO incumbent: {bo_trial.throughput_tps:.3f} TPS</text>',
            f'<text x="865" y="221" class="summary">p99: {bo_trial.p99_ms:.3f} ms · iteration {bo_trial.iteration}</text>',
            f'<text x="865" y="244" class="summary">Feasible trials: '
            f"{sum(t.feasible for t in trials if t.method == 'standard_bo')}/"
            f"{sum(1 for t in trials if t.method == 'standard_bo')}</text>",
            '<rect x="845" y="285" width="255" height="102" rx="10" fill="#FFF7ED" stroke="#FED7AA"/>',
            f'<text x="865" y="313" class="summary">Final random incumbent: {random_trial.throughput_tps:.3f} TPS</text>',
            f'<text x="865" y="336" class="summary">p99: {random_trial.p99_ms:.3f} ms · iteration {random_trial.iteration}</text>',
            f'<text x="865" y="359" class="summary">Feasible trials: '
            f"{sum(t.feasible for t in trials if t.method == 'random')}/"
            f"{sum(1 for t in trials if t.method == 'random')}</text>",
            '<text x="70" y="805" class="subtitle">No line is drawn before a method finds its first feasible configuration. '
            "This is one-seed development evidence, not a confidence-bounded effectiveness result.</text>",
            "</svg>",
        ]
    )
    path.write_text("\n".join(elements) + "\n", encoding="utf-8")


def write_report(trials: list[Trial], path: Path) -> None:
    summaries: dict[str, Trial] = {}
    for method in METHODS:
        summaries[method] = max(
            (trial for trial in trials if trial.method == method and trial.feasible),
            key=lambda item: item.throughput_tps,
        )
    random_trial = summaries["random"]
    bo_trial = summaries["standard_bo"]
    throughput_delta = bo_trial.throughput_tps - random_trial.throughput_tps
    throughput_percent = throughput_delta / random_trial.throughput_tps * 100.0
    p99_delta = random_trial.p99_ms - bo_trial.p99_ms
    body = f"""# Scale-10 sanity check

This report compares random search with the existing `standard_bo` arm for seed `{BASE_SEED}`.
In the current implementation, `standard_bo` uses unconstrained qLogNEHVI after four
scrambled-Sobol initialization trials. This is not the missing constrained qLogNEI thesis arm.

## Frozen conditions

- Dataset: pgbench scale 10
- Search space: `random_page_cost`, `work_mem`, `effective_io_concurrency`
- Warm-up: 120 seconds
- Measurement: 600 seconds
- Concurrency: 4
- Feasibility: zero failures and p99 no greater than {P99_SLO_MS:.0f} ms
- Random campaign: `{METHODS["random"]["campaign_id"]}`
- BO campaign: `{METHODS["standard_bo"]["campaign_id"]}`

## Exact results

| Method | Trials | Feasible | Best feasible TPS | Incumbent p99 | Found at iteration |
|---|---:|---:|---:|---:|---:|
| Random search | {sum(1 for t in trials if t.method == "random")} | {sum(t.feasible for t in trials if t.method == "random")} | {random_trial.throughput_tps:.6f} | {random_trial.p99_ms:.6f} ms | {random_trial.iteration} |
| Unconstrained BO (qLogNEHVI) | {sum(1 for t in trials if t.method == "standard_bo")} | {sum(t.feasible for t in trials if t.method == "standard_bo")} | {bo_trial.throughput_tps:.6f} | {bo_trial.p99_ms:.6f} ms | {bo_trial.iteration} |

The BO incumbent has **{throughput_delta:.6f} TPS ({throughput_percent:.2f}%) higher
throughput** and **{p99_delta:.3f} ms lower p99** than the random-search incumbent.

## Interpretation boundary

The result passes the requested one-seed sanity check because the methods are not practically
tied in this block. It does not establish general BO effectiveness: the dataset is cache-friendly,
the space has only three knobs, there is one seed, and the tested BO method is unconstrained
qLogNEHVI rather than the planned constrained qLogNEI versus qLogNEHVI comparison.

## Files

- `scale10-sanity-best-so-far.svg`: two-panel best-so-far figure
- `raw-results.csv`: every measured trial and source-artifact SHA-256
- `best-so-far.csv`: derived incumbent values for every iteration
- `manifest.json`: source and output lineage
"""
    path.write_text(body, encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    trials = load_trials()
    incumbent_rows = incumbents(trials)
    raw_csv = OUTPUT_DIR / "raw-results.csv"
    incumbent_csv = OUTPUT_DIR / "best-so-far.csv"
    svg = OUTPUT_DIR / "scale10-sanity-best-so-far.svg"
    report = OUTPUT_DIR / "report.md"
    manifest = OUTPUT_DIR / "manifest.json"
    write_raw_csv(trials, raw_csv)
    write_incumbent_csv(incumbent_rows, incumbent_csv)
    write_svg(incumbent_rows, trials, svg)
    write_report(trials, report)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "report_kind": "scale10_one_seed_random_vs_unconstrained_bo_sanity_check",
        "conditions": {
            "dataset_scale": 10,
            "seed": BASE_SEED,
            "warmup_seconds": 120,
            "measurement_seconds": 600,
            "concurrency": 4,
            "p99_slo_ms": P99_SLO_MS,
            "search_space": [
                "random_page_cost",
                "work_mem",
                "effective_io_concurrency",
            ],
        },
        "campaigns": {
            method: {
                "campaign_id": definition["campaign_id"],
                "label": definition["label"],
                "trial_count": sum(1 for trial in trials if trial.method == method),
            }
            for method, definition in METHODS.items()
        },
        "sources": [
            {
                "relative_path": str(trial.source_path.relative_to(ROOT)).replace("\\", "/"),
                "sha256": trial.source_sha256,
                "trial_id": trial.trial_id,
            }
            for trial in trials
        ],
        "outputs": {
            path.name: {"byte_size": path.stat().st_size, "sha256": sha256(path)}
            for path in (raw_csv, incumbent_csv, svg, report)
        },
    }
    manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(OUTPUT_DIR)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reproduce the historical scale-10 sanity report")
    parser.add_argument("--artifact-root", type=Path, default=ARTIFACTS)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    ARTIFACTS = args.artifact_root.resolve()
    OUTPUT_DIR = args.output_dir.resolve()
    main()
