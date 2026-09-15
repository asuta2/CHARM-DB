from __future__ import annotations

import copy
from pathlib import Path

from charmdb.analysis.multifidelity import analyze_phase_a_observations
from charmdb.protocol import load_manifest

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = load_manifest(ROOT / "experiments/thesis/manifests/multi-fidelity.json").payload
SEEDS = (88408573, 1418705027, 642754166)
CONTROL_POSITIONS = {1, 34, 66, 99, 131}


def _observations() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    global_position = 0
    for seed in SEEDS:
        candidate_index = 0
        for position in range(1, 132):
            global_position += 1
            control = position in CONTROL_POSITIONS
            if control:
                ratio = 1.0
                method = "postgresql_default"
            else:
                candidate_index += 1
                ratio = 0.70 + candidate_index * 0.004
                method = "bo_shared_initial" if candidate_index <= 12 else "random"
            rows.append(
                {
                    "primary_run_id": f"run-{global_position}",
                    "trial_id": f"trial-{global_position}",
                    "global_position": global_position,
                    "seed": seed,
                    "within_seed_position": position,
                    "evaluation_role": "DEFAULT_CONTROL" if control else "CANDIDATE",
                    "method": method,
                    "full_throughput_tps": 1000.0 * ratio,
                    "full_p99_ms": 30.0 - ratio,
                    "full_failures": 0,
                    "total_seconds": 1000.0,
                    "hard_gates_passed": True,
                    "authenticated": True,
                    "prefix_error": None,
                    "prefixes": {
                        "60": {
                            "throughput_tps": 900.0 * ratio,
                            "p99_ms": 31.0 - ratio,
                            "failures": 0,
                        },
                        "120": {
                            "throughput_tps": 950.0 * ratio,
                            "p99_ms": 30.5 - ratio,
                            "failures": 0,
                        },
                    },
                    "marker_relative_path": f"raw/{global_position}/measurement.json",
                    "marker_sha256": "a" * 64,
                    "trial_artifact_relative_path": f"raw/{global_position}/trial.json",
                    "trial_artifact_sha256": "b" * 64,
                }
            )
    return rows


def test_phase_a_adopts_only_when_every_seed_and_window_passes() -> None:
    analysis = analyze_phase_a_observations(_observations(), MANIFEST)

    assert analysis["physical_observations"] == 393
    assert analysis["physical_candidate_observations"] == 378
    assert analysis["physical_default_controls"] == 15
    assert analysis["shared_initialization_counted_once"] is True
    assert analysis["p99_is_promotion_constraint"] is False
    assert analysis["decision"] == "ELIGIBLE_FOR_OPTIONAL_PHASE_B"
    assert analysis["adopted"] is True
    seed_rows = [row for row in analysis["summaries"] if isinstance(row["seed"], int)]
    assert len(seed_rows) == 6
    assert all(row["spearman_control_relative_tps"] == 1.0 for row in seed_rows)
    assert all(row["top_quartile_false_rejections"] == 0 for row in seed_rows)


def test_phase_a_retains_one_top_quartile_false_rejection() -> None:
    rows = copy.deepcopy(_observations())
    candidates = [
        row
        for row in rows
        if row["seed"] == SEEDS[0] and row["method"] != "postgresql_default"
    ]
    best = max(candidates, key=lambda row: float(row["full_throughput_tps"]))
    best["prefixes"]["60"]["throughput_tps"] = 1.0  # type: ignore[index]

    analysis = analyze_phase_a_observations(rows, MANIFEST)

    assert analysis["decision"] == "REJECT_ADAPTIVE_MULTI_FIDELITY"
    assert analysis["adopted"] is False
    target = next(
        row
        for row in analysis["summaries"]
        if row["seed"] == SEEDS[0] and row["window_seconds"] == 60
    )
    assert target["top_quartile_false_rejections"] == 1
