from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from charmdb.optimization.design import (
    BO_METHODS,
    CONTROL_POSITIONS,
    PRIMARY_PARAMETER_ORDER,
    build_wave_a_plan,
    build_wave_a_schedule,
    decode_primary_point,
    encode_primary_configuration,
    export_primary_design,
    primary_candidate_design_payload,
    primary_candidate_design_sha256,
    primary_design_summary,
    schedule_sha256,
)
from charmdb.protocol import load_manifest

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "experiments/thesis/manifests/primary-comparison.json"


def test_wave_a_schedule_is_deterministic_and_exact() -> None:
    first = build_wave_a_schedule(MANIFEST)
    second = build_wave_a_schedule(MANIFEST)

    assert first == second
    assert schedule_sha256(first) == schedule_sha256(second)
    assert len(first) == 393
    assert [entry.global_position for entry in first] == list(range(1, 394))


def test_each_seed_has_five_controls_and_exact_method_budgets() -> None:
    by_seed: dict[int, list[object]] = defaultdict(list)
    for entry in build_wave_a_schedule(MANIFEST):
        by_seed[entry.seed].append(entry)

    assert len(by_seed) == 3
    for entries in by_seed.values():
        assert len(entries) == 131
        controls = [entry for entry in entries if entry.evaluation_role == "DEFAULT_CONTROL"]
        assert [entry.within_seed_position for entry in controls] == list(CONTROL_POSITIONS)
        counts = Counter(entry.method for entry in entries)
        assert counts == {
            "postgresql_default": 5,
            "random": 30,
            "sobol": 30,
            "bo_shared_initial": 12,
            "bo_qlognei_throughput": 18,
            "bo_qlognparego_multiobjective": 18,
            "bo_qlognehvi_multiobjective": 18,
        }
        shared = [entry for entry in entries if entry.method == "bo_shared_initial"]
        assert all(entry.shared_with_methods == tuple(BO_METHODS) for entry in shared)


def test_bo_adaptation_starts_only_after_all_shared_initial_points() -> None:
    for seed in {entry.seed for entry in build_wave_a_schedule(MANIFEST)}:
        entries = [entry for entry in build_wave_a_schedule(MANIFEST) if entry.seed == seed]
        last_shared = max(
            entry.within_seed_position for entry in entries if entry.shared_with_methods
        )
        first_adaptive = min(
            entry.within_seed_position
            for entry in entries
            if entry.method in BO_METHODS and entry.budget_position == 13
        )
        assert first_adaptive > last_shared


def test_summary_matches_frozen_design() -> None:
    """The design is invariant under P007 authorization.

    D041 promoted the manifest, so `execution_ready` is now true. Authorization
    is metadata only: the regenerated schedule and candidate design must still
    reproduce their frozen hashes, and Wave B must stay reserved.
    """
    summary = primary_design_summary(MANIFEST)

    assert summary["physical_observations"] == 393
    assert summary["schedule_sha256"] == summary["frozen_schedule_sha256"]
    assert summary["candidate_design_sha256"] == summary["frozen_candidate_design_sha256"]
    assert summary["wave_b_status"] == "reserved-not-authorized"
    assert summary["candidate_budget_per_method"] == 30
    assert summary["seeds"] == [88408573, 1418705027, 642754166]


def test_authorized_manifest_keeps_every_execution_precondition() -> None:
    """A `ready` PRIMARY manifest may never carry an unresolved decision."""
    summary = primary_design_summary(MANIFEST)
    payload = load_manifest(MANIFEST).payload

    if summary["status"] != "ready":
        assert summary["execution_ready"] is False
        return
    assert summary["execution_ready"] is True
    assert summary["unresolved_decisions"] == []
    assert payload["infrastructure_retry_policy"]["status"] == "frozen"
    assert payload["drift_interpretation"]["status"] == "frozen"
    assert payload["drift_interpretation"]["flag_is_campaign_killing"] is False
    assert payload["infrastructure_retry_policy"]["candidate_budget_consumed_by_retry"] is False
    assert all(payload["prerequisites"].values())


def test_fixed_candidate_design_is_unique_hash_frozen_and_separate_from_schedule() -> None:
    payload = load_manifest(MANIFEST).payload
    candidates = primary_candidate_design_payload(payload)
    plan = build_wave_a_plan(MANIFEST)

    assert len(candidates) == 216
    assert primary_candidate_design_sha256(payload) == (
        "6664b06c40a484c590b7c66cadbbce9ebad77a5dafbaa86d8bd1ba58719c143e"
    )
    for seed in payload["wave_a"]["seeds"]:
        for method, expected in (("random", 30), ("sobol", 30), ("bo_shared_initial", 12)):
            configurations = [
                item["configuration"]
                for item in candidates
                if item["seed"] == seed and item["method"] == method
            ]
            assert len(configurations) == expected
            assert len({str(sorted(item.items())) for item in configurations}) == expected
    assert sum(item.candidate is None for item in plan) == 177
    assert sum(item.candidate is not None for item in plan) == 216


def test_primary_codec_round_trips_generated_eight_dimensional_candidates() -> None:
    payload = load_manifest(MANIFEST).payload
    assert len(PRIMARY_PARAMETER_ORDER) == 8
    for item in primary_candidate_design_payload(payload)[:20]:
        configuration = item["configuration"]
        assert isinstance(configuration, dict)
        encoded = encode_primary_configuration(configuration, payload)
        decoded = decode_primary_point(encoded, payload)
        assert decoded.configuration == configuration
        assert len(decoded.vector) == 8

    low = decode_primary_point([0.0] * 8, payload)
    high = decode_primary_point([1.0] * 8, payload)
    for name in PRIMARY_PARAMETER_ORDER:
        specification = payload["search_space"]["parameters"][name]
        assert float(low.configuration[name]) == float(specification["lower"])
        assert float(high.configuration[name]) == float(specification["upper"])


def test_primary_design_export_retains_unmaterialized_adaptive_slots() -> None:
    output = ROOT / "artifacts-newpc/v2/primary-comparison/test-primary-design.json"
    result = export_primary_design(MANIFEST, output)
    exported = json.loads(output.read_text(encoding="utf-8"))
    try:
        assert result["candidate_design_sha256"] == (
            "6664b06c40a484c590b7c66cadbbce9ebad77a5dafbaa86d8bd1ba58719c143e"
        )
        assert len(exported["entries"]) == 393
        assert sum(item["requested_configuration"] is None for item in exported["entries"]) == 177
    finally:
        output.unlink(missing_ok=True)
