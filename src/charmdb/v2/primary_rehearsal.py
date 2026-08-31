"""Result-blind rehearsal of the Wave A analysis and reporting pipeline.

Wave A costs 181.514 restore-inclusive hours. Discovering afterwards that the
frozen analysis, table, or figure code cannot consume its own ledger would be
unrecoverable, so this module exercises the complete path on synthetic ledgers
before the campaign exists.

Nothing here is evidence. The values are produced by a documented SHA-256
pseudo-random formula, never by a benchmark, and every rendered artifact is
labelled ``synthetic``. The rehearsal cannot read, create, or modify a primary
campaign, and it never touches the target database.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from itertools import pairwise
from pathlib import Path
from typing import Any

from charmdb.v2.primary_design import (
    PRIMARY_MANIFEST,
    PRIMARY_PARAMETER_ORDER,
    build_wave_a_plan,
    decode_primary_point,
)
from charmdb.v2.primary_reporting import (
    LOGICAL_BUDGET,
    SHARED_INITIAL_METHOD,
    SHARED_INITIAL_SIZE,
    render_primary_report,
)
from charmdb.v2.protocol import PRIMARY_SEARCH_METHODS, load_manifest

REHEARSAL_SCENARIOS: tuple[str, ...] = ("nominal", "failures-and-retries", "drift-flagged")
DEFAULT_REHEARSAL_DIRNAME = "analysis-rehearsal-synthetic"

_METHOD_BASE_TPS: dict[str, float] = {
    "random": 2400.0,
    "sobol": 2420.0,
    "bo_shared_initial": 2430.0,
    "bo_qlognei_throughput": 2500.0,
    "bo_qlognparego_multiobjective": 2470.0,
    "bo_qlognehvi_multiobjective": 2480.0,
}
_METHOD_BASE_P99: dict[str, float] = {
    "random": 31.0,
    "sobol": 30.6,
    "bo_shared_initial": 30.4,
    "bo_qlognei_throughput": 29.8,
    "bo_qlognparego_multiobjective": 29.2,
    "bo_qlognehvi_multiobjective": 29.0,
}

SYNTHETIC_NOTICE = (
    "SYNTHETIC REHEARSAL OUTPUT - NOT EVIDENCE. Objective values come from a "
    "documented SHA-256 pseudo-random formula in src/charmdb/v2/primary_rehearsal.py "
    "and were never measured. These files exist only to prove that the frozen Wave A "
    "analysis, table, and figure code consumes a complete 393-slot ledger before the "
    "181.514-hour campaign is authorized."
)


def _unit(label: str) -> float:
    """Deterministic pseudo-random value in [0,1) derived from a text label."""
    return int.from_bytes(hashlib.sha256(label.encode()).digest()[:6], "big") / float(1 << 48)


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def synthetic_wave_a_rows(
    scenario: str, manifest_path: Path = PRIMARY_MANIFEST
) -> list[dict[str, Any]]:
    """Build a complete synthetic 393-slot ledger for one rehearsal scenario.

    The schedule identity, seeds, methods, budget positions, and shared
    initialization sharing come from the frozen plan; only the outcome fields
    are synthesized.
    """
    if scenario not in REHEARSAL_SCENARIOS:
        raise ValueError(f"unsupported rehearsal scenario {scenario!r}")
    payload = load_manifest(manifest_path).payload
    seeds = [int(seed) for seed in payload["wave_a"]["seeds"]]
    rows: list[dict[str, Any]] = []
    for item in build_wave_a_plan(manifest_path):
        entry = item.schedule
        position = entry.global_position
        control = entry.method == "postgresql_default"
        fraction = (entry.within_seed_position - 1) / 130.0
        if control:
            throughput = 2700.0 + 40.0 * _unit(f"{scenario}/control-tps/{position}")
            p99 = 29.0 + 1.2 * _unit(f"{scenario}/control-p99/{position}")
            if scenario == "drift-flagged":
                throughput -= 190.0 * fraction
                p99 += 8.5 * fraction
        else:
            throughput = _METHOD_BASE_TPS[entry.method] + 300.0 * _unit(
                f"{scenario}/tps/{position}"
            )
            p99 = _METHOD_BASE_P99[entry.method] + 7.0 * _unit(f"{scenario}/p99/{position}")
        candidate = item.candidate
        if candidate is None and not control:
            # Adaptive Bayesian slots are materialized at run time, so the
            # rehearsal supplies a synthetic in-bounds vector for them.
            candidate = decode_primary_point(
                [
                    _unit(f"{scenario}/vector/{position}/{index}")
                    for index in range(len(PRIMARY_PARAMETER_ORDER))
                ],
                payload,
            )
        status = "COMPLETED"
        failures = 0
        attempts = 0
        if scenario == "failures-and-retries" and not control:
            if position % 37 == 0:
                status = "CANDIDATE_FAILED"
            elif position % 89 == 0:
                status = "INFRASTRUCTURE_EXHAUSTED"
                attempts = 3
            elif position % 61 == 0:
                failures = 4
            elif position % 17 == 0:
                attempts = 1
        terminal = status == "COMPLETED"
        rows.append(
            {
                "primary_run_id": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb-rehearsal:{scenario}:{position}")
                ),
                "global_position": position,
                "seed_index": entry.seed_index,
                "seed": entry.seed,
                "within_seed_position": entry.within_seed_position,
                "evaluation_role": entry.evaluation_role,
                "method": entry.method,
                "budget_position": entry.budget_position,
                "shared_with_methods": list(entry.shared_with_methods),
                "status": status,
                "infrastructure_attempts": attempts,
                "candidate_vector": (list(candidate.vector) if candidate is not None else None),
                "requested_configuration": (
                    dict(candidate.configuration)
                    if candidate is not None
                    else dict(payload["postgresql_default_configuration"])
                ),
                "objective_values": (
                    {"throughput_tps": throughput, "p99_ms": p99} if terminal else None
                ),
                "constraint_values": {"failures": failures} if terminal else None,
            }
        )
    if len(rows) != 393 or sorted({int(row["seed"]) for row in rows}) != sorted(seeds):
        raise RuntimeError("synthetic rehearsal ledger does not match the frozen Wave A plan")
    return rows


def _check(condition: bool, message: str, violations: list[str]) -> None:
    if not condition:
        violations.append(message)


def rehearsal_invariants(analysis: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    """Return every structural violation found in a rendered analysis payload."""
    violations: list[str] = []
    seed_count = len(payload["wave_a"]["seeds"])
    _check(
        sum(analysis["terminal_counts"].values()) == 393,
        "analysis did not account for all 393 physical observations",
        violations,
    )
    _check(
        len(analysis["slot_trajectories"]) == seed_count * len(PRIMARY_SEARCH_METHODS),
        "one trajectory per seed and method is required",
        violations,
    )
    for trajectory in analysis["slot_trajectories"]:
        slots = trajectory["slots"]
        method = str(trajectory["method"])
        _check(
            [slot["slot"] for slot in slots] == list(range(1, LOGICAL_BUDGET + 1)),
            f"{method} trajectory is not a contiguous 1-30 logical budget",
            violations,
        )
        shared = [slot for slot in slots if slot["physical_method"] == SHARED_INITIAL_METHOD]
        expected_shared = SHARED_INITIAL_SIZE if method not in {"random", "sobol"} else 0
        _check(
            len(shared) == expected_shared,
            f"{method} used {len(shared)} shared initialization slots, expected {expected_shared}",
            violations,
        )
        hypervolumes = [float(slot["hypervolume"]) for slot in slots]
        _check(
            all(later >= earlier - 1e-9 for earlier, later in pairwise(hypervolumes)),
            f"{method} hypervolume trajectory is not monotone non-decreasing",
            violations,
        )
        best = [slot["best_throughput_tps"] for slot in slots if slot["best_throughput_tps"]]
        _check(
            all(later >= earlier - 1e-9 for earlier, later in pairwise(best)),
            f"{method} best-throughput trajectory decreased",
            violations,
        )
        worst = [slot["minimum_p99_ms"] for slot in slots if slot["minimum_p99_ms"]]
        _check(
            all(later <= earlier + 1e-9 for earlier, later in pairwise(worst)),
            f"{method} minimum-p99 trajectory increased",
            violations,
        )
    families = {str(item["metric"]) for item in analysis["pairwise_comparisons"]}
    for family in families:
        pairs = [item for item in analysis["pairwise_comparisons"] if item["metric"] == family]
        _check(len(pairs) == 10, f"endpoint {family} does not have ten method pairs", violations)
        for pair in pairs:
            _check(
                float(pair["holm_adjusted_p_value_within_metric_family"])
                >= float(pair["permutation_p_value"]) - 1e-12,
                f"Holm adjustment reduced a p-value in {family}",
                violations,
            )
    _check(len(families) == 5, "five registered endpoint families are required", violations)
    _check(
        len(analysis["drift_flags"]) == seed_count,
        "every seed must report a drift record",
        violations,
    )
    _check(
        all(item.get("campaign_killing") is False for item in analysis["drift_flags"]),
        "drift flags must never be campaign-killing",
        violations,
    )
    _check(
        len(analysis["control_series"]) == seed_count * 5,
        "five default controls per seed are required",
        violations,
    )
    counts = {str(item["method"]): item for item in analysis["failure_accounting"]}
    for method in PRIMARY_SEARCH_METHODS:
        item = counts[method]
        total = (
            int(item["completed_slots"])
            + int(item["candidate_failed_slots"])
            + int(item["infrastructure_exhausted_slots"])
        )
        _check(
            total == LOGICAL_BUDGET * seed_count,
            f"{method} terminal slot accounting does not sum to its logical budget",
            violations,
        )
    return violations


def run_analysis_rehearsal(
    output_dir: Path,
    scenarios: tuple[str, ...] = REHEARSAL_SCENARIOS,
    manifest_path: Path = PRIMARY_MANIFEST,
) -> dict[str, Any]:
    """Run every rehearsal scenario end to end and return a durable summary.

    Each scenario is analysed, checked against structural invariants, rendered,
    and then rendered a second time into a scratch directory to prove that the
    report is byte-deterministic. The scratch copy is removed afterwards.
    """
    from charmdb.v2.primary_execution import analyze_primary_observations

    payload = load_manifest(manifest_path).payload
    root = output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "README-SYNTHETIC.md").write_text(
        f"# Synthetic analysis rehearsal\n\n{SYNTHETIC_NOTICE}\n", encoding="utf-8"
    )
    scratch = root / ".determinism-check"
    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        rows = synthetic_wave_a_rows(scenario, manifest_path)
        analysis = analyze_primary_observations(rows, payload)
        violations = rehearsal_invariants(analysis, payload)
        context = {
            "campaign_id": f"synthetic-rehearsal-{scenario}",
            "primary_block_id": f"synthetic-rehearsal-{scenario}",
            "evidence_role": "INFRASTRUCTURE",
            "synthetic": True,
            "notice": SYNTHETIC_NOTICE,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "schedule_sha256": payload["execution_schedule"]["schedule_sha256"],
            "candidate_design_sha256": payload["shared_bo_initial_design"][
                "candidate_design_sha256"
            ],
            "benchmark_profile_id": payload["benchmark_profile_id"],
            "analysis_sha256": _canonical_sha256(analysis),
        }
        rendered = render_primary_report(analysis, context, root / scenario)
        repeat = render_primary_report(analysis, context, scratch / scenario)
        deterministic = [item["sha256"] for item in rendered["files"]] == [
            item["sha256"] for item in repeat["files"]
        ] and rendered["payload_sha256"] == repeat["payload_sha256"]
        if not deterministic:
            violations.append(f"{scenario} report rendering is not byte-deterministic")
        results.append(
            {
                "scenario": scenario,
                "outcome": analysis["outcome"],
                "analysis_sha256": context["analysis_sha256"],
                "report_payload_sha256": rendered["payload_sha256"],
                "deterministic_rerender": deterministic,
                "terminal_counts": analysis["terminal_counts"],
                "physical_valid_candidates": analysis["physical_valid_candidates"],
                "valid_default_controls": analysis["valid_default_controls"],
                "flagged_seeds": sum(bool(item["flagged"]) for item in analysis["drift_flags"]),
                "pareto_front_size": len(analysis["pareto_front"]),
                "retained_infrastructure_attempts": sum(
                    int(item["retained_infrastructure_attempts"])
                    for item in analysis["failure_accounting"]
                ),
                "file_count": rendered["file_count"],
                "total_bytes": rendered["total_bytes"],
                "output_dir": rendered["output_dir"],
                "violations": violations,
            }
        )
    if scratch.exists():
        shutil.rmtree(scratch)
    summary = {
        "kind": "thesis-protocol-v2-primary-analysis-rehearsal",
        "evidence_role": "INFRASTRUCTURE",
        "synthetic": True,
        "notice": SYNTHETIC_NOTICE,
        "creates_primary_campaign": False,
        "touches_target_database": False,
        "schedule_sha256": payload["execution_schedule"]["schedule_sha256"],
        "candidate_design_sha256": payload["shared_bo_initial_design"]["candidate_design_sha256"],
        "scenarios": results,
        "passed": all(not item["violations"] for item in results),
        "output_dir": str(root),
    }
    summary["result_sha256"] = _canonical_sha256(
        {key: value for key, value in summary.items() if key != "output_dir"}
    )
    encoded = (json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
    (root / "rehearsal-summary.json").write_bytes(encoded)
    summary["summary_file_sha256"] = hashlib.sha256(encoded).hexdigest()
    return summary
