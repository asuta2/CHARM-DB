from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from charmdb.reporting.figures import contained, f4_deltas, load_evidence, render
from charmdb.reporting.primary import SEED_COLORS, figure_default_drift


def test_five_seed_drift_never_recycles_a_color_and_preserves_legacy_mode() -> None:
    a = {
        "unified_five_seed_cohort": True,
        "report_scope": "final five-seed",
        "drift_flags": [],
        "control_series": [
            {
                "seed": seed,
                "valid": True,
                "within_seed_position": position,
                "throughput_tps": 3000 + seed + position,
                "p99_ms": 26 + seed / 10,
            }
            for seed in range(5)
            for position in (1, 34, 66, 99, 131)
        ],
    }
    root = ET.fromstring(figure_default_drift(a))
    series = root.findall("{http://www.w3.org/2000/svg}polyline")
    assert len(series) == 10
    assert {x.attrib["stroke"] for x in series} == set(SEED_COLORS)
    assert len({x.attrib["stroke-dasharray"] for x in series}) == 5
    legacy = ET.fromstring(figure_default_drift(a, legacy_colors=True))
    old_series = legacy.findall("{http://www.w3.org/2000/svg}polyline")
    assert len({x.attrib["stroke"] for x in old_series}) == 3
    assert all("stroke-dasharray" not in x.attrib for x in old_series)


def test_confirmation_pairs_each_candidate_with_its_own_block() -> None:
    observations = []
    for block in range(1, 5):
        for treatment in ("DEFAULT", "T", "L", "H", "E"):
            observations.append(
                {
                    "repetition_block": block,
                    "treatment": treatment,
                    "throughput_tps": 100 * block * (1 if treatment == "DEFAULT" else 1.1),
                    "p99_ms": 20 + block + (0 if treatment == "DEFAULT" else -2),
                }
            )
    e = {"confirmation": {"observations": observations}}
    rows = f4_deltas(e)
    assert len(rows) == 16
    assert all(x["tps_percent"] == pytest.approx(10) for x in rows)
    assert all(x["p99_delta_ms"] == -2 for x in rows)
    observations[-1]["treatment"] = "H"
    with pytest.raises(ValueError, match="each treatment exactly once"):
        f4_deltas(e)


def test_evidence_tampering_is_rejected_before_rendering(tmp_path: Path) -> None:
    relative = "primary-wave-b/final-five-seed-analysis.json"
    path = tmp_path / relative
    path.parent.mkdir()
    path.write_text("{}", encoding="utf-8")
    (tmp_path / "evidence-sha256-manifest.json").write_text(
        json.dumps({"files": [{"relative_path": relative, "sha256": "0" * 64}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Evidence hash mismatch"):
        load_evidence(tmp_path)


def test_output_cannot_overwrite_sources_or_an_existing_report(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="separate"):
        render(source, source / "report")
    with pytest.raises(ValueError, match="escapes"):
        contained(source, "../outside.json")
    output = tmp_path / "output"
    output.mkdir()
    (output / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="new or empty"):
        render(source, output)
    assert (output / "keep.txt").read_text() == "keep"
