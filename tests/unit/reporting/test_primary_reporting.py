from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from charmdb.analysis.rehearsal import synthetic_wave_a_rows
from charmdb.campaigns.primary import analyze_primary_observations
from charmdb.optimization.design import BO_METHODS, PRIMARY_MANIFEST
from charmdb.protocol import PRIMARY_SEARCH_METHODS, load_manifest
from charmdb.reporting.primary import (
    FIGURES,
    LOGICAL_BUDGET,
    METHOD_LABELS,
    SHARED_INITIAL_METHOD,
    SHARED_INITIAL_SIZE,
    control_series,
    failure_accounting,
    logical_slot_trajectories,
    primary_tables,
    render_primary_report,
    valid_primary_row,
)

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / PRIMARY_MANIFEST
PAYLOAD = load_manifest(MANIFEST).payload
CONTEXT = {
    "campaign_id": "test-campaign",
    "primary_block_id": "test-block",
    "manifest_sha256": "0" * 64,
    "schedule_sha256": PAYLOAD["execution_schedule"]["schedule_sha256"],
    "candidate_design_sha256": PAYLOAD["shared_bo_initial_design"]["candidate_design_sha256"],
    "benchmark_profile_id": PAYLOAD["benchmark_profile_id"],
    "analysis_sha256": "1" * 64,
}


def _rows(scenario: str = "nominal") -> list[dict[str, object]]:
    return synthetic_wave_a_rows(scenario, MANIFEST)


def test_bayesian_slots_read_shared_initialization_once_and_random_never_does() -> None:
    trajectories = logical_slot_trajectories(_rows(), PAYLOAD)
    by_method: dict[str, list[dict[str, object]]] = {}
    for trajectory in trajectories:
        by_method.setdefault(str(trajectory["method"]), []).append(trajectory)

    for method in BO_METHODS:
        for trajectory in by_method[method]:
            slots = trajectory["slots"]
            assert isinstance(slots, list)
            shared = [item for item in slots if item["physical_method"] == SHARED_INITIAL_METHOD]
            assert [item["slot"] for item in shared] == list(range(1, SHARED_INITIAL_SIZE + 1))
            adaptive = [item for item in slots if item["physical_method"] == method]
            assert [item["slot"] for item in adaptive] == list(
                range(SHARED_INITIAL_SIZE + 1, LOGICAL_BUDGET + 1)
            )
    for method in ("random", "sobol"):
        for trajectory in by_method[method]:
            slots = trajectory["slots"]
            assert isinstance(slots, list)
            assert all(item["physical_method"] == method for item in slots)


def test_shared_initialization_row_is_physically_measured_once() -> None:
    rows = _rows()
    shared = [row for row in rows if row["method"] == SHARED_INITIAL_METHOD]
    assert len(shared) == SHARED_INITIAL_SIZE * len(PAYLOAD["wave_a"]["seeds"])
    identifiers = {str(row["primary_run_id"]) for row in shared}
    assert len(identifiers) == len(shared)
    for row in shared:
        assert sorted(row["shared_with_methods"]) == sorted(BO_METHODS)


def test_invalid_slot_consumes_its_budget_and_carries_the_previous_best_forward() -> None:
    rows = _rows()
    target = next(
        row
        for row in rows
        if row["method"] == "random" and row["seed"] == PAYLOAD["wave_a"]["seeds"][0]
        and row["budget_position"] == 5
    )
    target["status"] = "CANDIDATE_FAILED"
    target["objective_values"] = None
    target["constraint_values"] = None
    assert valid_primary_row(target) is False

    trajectory = next(
        item
        for item in logical_slot_trajectories(rows, PAYLOAD)
        if item["method"] == "random" and item["seed"] == PAYLOAD["wave_a"]["seeds"][0]
    )
    slots = trajectory["slots"]
    assert isinstance(slots, list)
    assert len(slots) == LOGICAL_BUDGET
    assert slots[4]["valid"] is False
    assert slots[4]["throughput_tps"] is None
    assert slots[4]["best_throughput_tps"] == slots[3]["best_throughput_tps"]
    assert slots[4]["hypervolume"] == slots[3]["hypervolume"]
    assert trajectory["valid_slots"] == LOGICAL_BUDGET - 1


def test_failure_accounting_covers_every_logical_slot_and_retains_retries() -> None:
    rows = _rows("failures-and-retries")
    accounting = {str(item["method"]): item for item in failure_accounting(rows, PAYLOAD)}
    seeds = len(PAYLOAD["wave_a"]["seeds"])
    for method in PRIMARY_SEARCH_METHODS:
        item = accounting[method]
        assert item["logical_slots"] == LOGICAL_BUDGET * seeds
        assert (
            item["completed_slots"]
            + item["candidate_failed_slots"]
            + item["infrastructure_exhausted_slots"]
            == LOGICAL_BUDGET * seeds
        )
        assert (
            item["valid_candidate_observations"] + item["completed_but_invalid_slots"]
            == item["completed_slots"]
        )
    assert sum(int(accounting[m]["retained_infrastructure_attempts"]) for m in accounting) > 0
    assert accounting["postgresql_default"]["logical_slots"] == 0


def test_control_series_requires_five_controls_per_seed() -> None:
    rows = _rows()
    series = control_series(rows, PAYLOAD)
    assert len(series) == 5 * len(PAYLOAD["wave_a"]["seeds"])
    assert [item["within_seed_position"] for item in series[:5]] == [1, 34, 66, 99, 131]
    with pytest.raises(ValueError, match="exactly 15 default controls"):
        control_series([row for row in rows if row["within_seed_position"] != 66], PAYLOAD)


def test_report_is_complete_deterministic_and_bom_free(tmp_path: Path) -> None:
    analysis = analyze_primary_observations(_rows(), PAYLOAD)
    first = render_primary_report(analysis, CONTEXT, tmp_path / "first")
    second = render_primary_report(analysis, CONTEXT, tmp_path / "second")

    assert first["payload_sha256"] == second["payload_sha256"]
    assert [item["sha256"] for item in first["files"]] == [
        item["sha256"] for item in second["files"]
    ]
    written = {item["path"] for item in first["files"]}
    assert "primary-report.md" in written
    assert {f"figures/{name}" for name in FIGURES} <= written
    assert {f"tables/{name}.csv" for name in primary_tables(analysis)} <= written
    for item in first["files"]:
        data = (tmp_path / "first" / item["path"]).read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf")
        assert not data.startswith(b"\xff\xfe")
        data.decode("utf-8")


def test_report_states_the_three_seed_and_non_killing_drift_guards(tmp_path: Path) -> None:
    analysis = analyze_primary_observations(_rows(), PAYLOAD)
    render_primary_report(analysis, CONTEXT, tmp_path)
    markdown = (tmp_path / "primary-report.md").read_text(encoding="utf-8")

    assert "never be described as five-seed confirmation" in markdown
    assert "consumes no candidate slot" in markdown
    assert "never terminate or discard Wave A" in markdown
    assert "append-only deviation record" in markdown
    for label in METHOD_LABELS.values():
        assert label in markdown


def test_every_figure_is_valid_xml_and_labels_all_methods(tmp_path: Path) -> None:
    analysis = analyze_primary_observations(_rows(), PAYLOAD)
    render_primary_report(analysis, CONTEXT, tmp_path)
    for name in FIGURES:
        path = tmp_path / "figures" / name
        ElementTree.parse(path)
        assert path.read_text(encoding="utf-8").startswith("<svg xmlns=")
    pareto = (tmp_path / "figures" / "figure-pareto-tps-vs-p99.svg").read_text(encoding="utf-8")
    for label in METHOD_LABELS.values():
        assert label in pareto
    assert "PostgreSQL default control" in pareto
