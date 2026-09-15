from copy import deepcopy

from charmdb.campaigns.f4 import F4_MANIFEST, analyze_f4_observations, build_f4_plan
from charmdb.protocol import load_manifest


def _rows() -> list[dict[str, object]]:
    outcomes = {
        "DEFAULT": (3000.0, 28.0),
        "T": (3060.0, 27.2),
        "L": (2940.0, 25.5),
        "H": (3120.0, 27.5),
        "E": (3030.0, 26.5),
    }
    rows: list[dict[str, object]] = []
    for slot in build_f4_plan():
        tps, p99 = outcomes[slot.treatment]
        rows.append(
            {
                "physical_position": slot.physical_position,
                "repetition_block": slot.repetition_block,
                "within_block_position": slot.within_block_position,
                "treatment": slot.treatment,
                "source_primary_run_id": slot.source_primary_run_id,
                "random_seed": slot.random_seed,
                "requested_configuration": slot.configuration,
                "status": "COMPLETED",
                "infrastructure_attempts": 1,
                "authenticated": True,
                "valid": True,
                "objective_values": {"throughput_tps": tps, "p99_ms": p99},
                "lifecycle_seconds": 1666.0,
            }
        )
    return rows


def test_f4_plan_has_exact_common_seed_blocks_and_source_identity() -> None:
    plan = build_f4_plan()

    assert len(plan) == 20
    assert [slot.treatment for slot in plan] == [
        "T",
        "E",
        "DEFAULT",
        "L",
        "H",
        "E",
        "L",
        "DEFAULT",
        "H",
        "T",
        "L",
        "H",
        "DEFAULT",
        "T",
        "E",
        "H",
        "T",
        "DEFAULT",
        "E",
        "L",
    ]
    for block in range(1, 5):
        rows = [slot for slot in plan if slot.repetition_block == block]
        assert len({slot.random_seed for slot in rows}) == 1
        assert rows[2].treatment == "DEFAULT"
        assert rows[2].source_primary_run_id is None
        assert all(
            row.source_primary_run_id is not None for row in rows if row.treatment != "DEFAULT"
        )


def test_f4_analysis_selects_only_candidate_passing_all_frozen_gates() -> None:
    payload = load_manifest(F4_MANIFEST).payload
    analysis = analyze_f4_observations(_rows(), [], payload)

    assert analysis["decision"] == "CONFIRMED_CHAMPION"
    assert analysis["selected_treatment"] == "E"
    gates = {row["treatment"]: row["gates"] for row in analysis["candidate_summaries"]}
    assert gates["T"]["latency_vs_default"] is False
    assert gates["L"]["frontier_throughput"] is False
    assert gates["H"]["latency_vs_default"] is False
    assert all(gates["E"].values())
    assert analysis["inference_limits"]["apply_best_authorized"] is False


def test_f4_invalid_observation_forces_no_confirmed_champion() -> None:
    payload = load_manifest(F4_MANIFEST).payload
    rows = deepcopy(_rows())
    rows[0]["valid"] = False
    rows[0]["status"] = "CANDIDATE_FAILED"

    analysis = analyze_f4_observations(rows, [], payload)

    assert analysis["decision"] == "NO_CONFIRMED_CHAMPION"
    assert analysis["selected_treatment"] is None
    assert analysis["candidate_summaries"] == []
