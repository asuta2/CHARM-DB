"""Closeout corrections must not double-count shared initialization or first attempts."""

import runpy
from pathlib import Path

import pytest

AUDIT = runpy.run_path(
    str(Path(__file__).resolve().parents[3] / "scripts/audit_thesis_closeout.py")
)


def test_retry_accounting_counts_physical_failure_once_and_attributes_shared_methods() -> None:
    runs = [
        {"primary_run_id": "shared", "method": "shared", "shared_with_methods": ["bo1", "bo2"]},
        {"primary_run_id": "random", "method": "random", "shared_with_methods": []},
    ]
    attempts = [
        {"primary_run_id": "shared", "status": "FAILED"},
        {"primary_run_id": "shared", "status": "COMPLETED"},
        {"primary_run_id": "random", "status": "COMPLETED"},
    ]
    result = {r["scope"]: r for r in AUDIT["safety_correction"](runs, attempts)}
    assert result["ALL_PHYSICAL"]["total_attempts_including_first"] == 3
    assert result["ALL_PHYSICAL"]["slots_retried"] == 1
    assert result["ALL_PHYSICAL"]["retained_failed_attempts"] == 1
    assert result["random"]["slots_retried"] == 0
    assert result["bo1"]["slots_retried"] == result["bo2"]["slots_retried"] == 1


def test_changed_manifest_is_rejected_before_report_creation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "evidence-sha256-manifest.json").write_text("{}", encoding="utf-8")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="trust-anchor mismatch"):
        AUDIT["audit"](source, output)
    assert not output.exists()


def test_evidence_paths_cannot_escape_source(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes root"):
        AUDIT["contained"](tmp_path.resolve(), "../outside.json")
