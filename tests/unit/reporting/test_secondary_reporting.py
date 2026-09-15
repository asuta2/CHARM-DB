from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any

import pytest

from charmdb.analysis.rehearsal import synthetic_wave_a_rows
from charmdb.campaigns.primary import analyze_primary_observations
from charmdb.optimization.design import PRIMARY_MANIFEST
from charmdb.protocol import load_manifest
from charmdb.reporting.secondary import (
    SECONDARY_EVIDENCE_ROLE,
    export_secondary_report,
    load_terminal_primary_export,
    render_secondary_report,
    secondary_tables,
)

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / PRIMARY_MANIFEST
PAYLOAD = load_manifest(MANIFEST).payload


def _sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _export(scenario: str = "nominal") -> dict[str, Any]:
    rows = synthetic_wave_a_rows(scenario, MANIFEST)
    analysis = analyze_primary_observations(rows, PAYLOAD)
    return {
        "campaign_id": "synthetic-campaign",
        "primary_block_id": "synthetic-block",
        "analysis_sha256": _sha256(analysis),
        "analysis_sha256_semantics": "canonical JSON payload hash, not file hash",
        "analysis": analysis,
        "history": {"runs": rows},
    }


def test_secondary_tables_cover_registered_formulas_without_new_endpoint() -> None:
    tables = secondary_tables(_export())

    assert len(tables["s1-candidate-control-contrasts"]) == 3 * 5 * 30
    assert len(tables["s1-quadrant-shares"]) == 3 * 5
    assert len(tables["s2-adaptive-gain"]) == 3 * 5
    assert len(tables["s4-seed-consistency"]) == 6 * 3 * 5
    assert len(tables["s4-leave-one-seed-out"]) == 6 * 4 * 5
    assert len(tables["s6-search-diversity"]) == 3 * 5
    assert len(tables["s8-summary-dashboard"]) == 5

    for row in tables["s1-quadrant-shares"]:
        assert sum(
            int(row[name]) for name in ("better_both", "tps_only", "p99_only", "worse_both")
        ) == int(row["valid_observations"])
    assert all(
        set(row["normalized_configuration"]) == set(PAYLOAD["search_space"]["parameter_order"])
        for row in tables["s3-per-seed-physical-pareto"]
    )


def test_shared_initialization_is_logical_but_wall_clock_is_physical() -> None:
    tables = secondary_tables(_export())
    contrasts = tables["s1-candidate-control-contrasts"]
    seed = int(PAYLOAD["wave_a"]["seeds"][0])

    shared_ids = {
        row["primary_run_id"]
        for row in contrasts
        if row["seed"] == seed and row["physical_method"] == "bo_shared_initial"
    }
    assert len(shared_ids) == 12
    assert (
        sum(
            row["seed"] == seed and row["physical_method"] == "bo_shared_initial"
            for row in contrasts
        )
        == 36
    )

    wall = tables["s8-physical-wall-clock"]
    shared = [
        row for row in wall if row["seed"] == seed and row["physical_method"] == "bo_shared_initial"
    ]
    assert len(shared) == 1
    assert shared[0]["completed_observations"] == 12


def test_report_is_deterministic_valid_xml_and_explicitly_exploratory(tmp_path: Path) -> None:
    export = _export("drift-flagged")
    source = tmp_path / "primary-analysis.json"
    source.write_text(json.dumps(export, indent=2) + "\n", encoding="utf-8")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    first = render_secondary_report(
        export,
        tmp_path / "first",
        source_path=source,
        source_file_sha256=source_hash,
    )
    second = render_secondary_report(
        export,
        tmp_path / "second",
        source_path=source,
        source_file_sha256=source_hash,
    )

    assert first["payload_sha256"] == second["payload_sha256"]
    assert [row["sha256"] for row in first["files"]] == [row["sha256"] for row in second["files"]]
    index = json.loads((tmp_path / "first" / "secondary-report-index.json").read_text())
    assert index["evidence_role"] == SECONDARY_EVIDENCE_ROLE
    assert index["confirmatory_endpoints_changed"] is False
    assert index["candidate_rows_are_inferential_units"] is False
    assert index["rejected_display"].startswith("S7")
    markdown = (tmp_path / "first" / "secondary-report.md").read_text()
    assert "EXPLORATORY / DESCRIPTIVE ONLY" in markdown
    assert "no knee tag" in markdown
    assert "No composite score" in markdown
    assert str(PAYLOAD["wave_a"]["seeds"][0]) in markdown
    for figure in index["figures"]:
        ElementTree.parse(tmp_path / "first" / "figures" / figure)


def test_export_loader_rejects_analysis_hash_tampering(tmp_path: Path) -> None:
    export = _export()
    path = tmp_path / "primary-analysis.json"
    path.write_text(json.dumps(export) + "\n", encoding="utf-8")
    loaded, file_hash = load_terminal_primary_export(path)
    assert loaded["analysis_sha256"] == export["analysis_sha256"]
    assert file_hash == hashlib.sha256(path.read_bytes()).hexdigest()

    export["analysis"]["outcome"] = "tampered"
    path.write_text(json.dumps(export) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="payload SHA-256 mismatch"):
        export_secondary_report(path, tmp_path / "should-not-exist")
