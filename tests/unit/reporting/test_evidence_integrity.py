from __future__ import annotations

import json
from pathlib import Path

import pytest

from charmdb.reporting.integrity import build_evidence_manifest


def test_evidence_manifest_is_deterministic_excludes_itself_and_flags_utf16(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    root = Path("artifacts-newpc/v2/example")
    root.mkdir(parents=True)
    (root / "result.json").write_text('{"value":1}\n', encoding="utf-8")
    (root / "legacy.csv").write_text("name,value\na,1\n", encoding="utf-16")

    first = build_evidence_manifest(root)
    second = build_evidence_manifest(root)
    payload = json.loads((root / "evidence-sha256-manifest.json").read_text(encoding="utf-8"))

    assert first["manifest_payload_sha256"] == second["manifest_payload_sha256"]
    assert first["file_sha256"] == second["file_sha256"]
    assert first["file_count"] == 2
    assert first["non_utf8_bom_files"] == ["legacy.csv"]
    assert {item["relative_path"] for item in payload["files"]} == {
        "legacy.csv",
        "result.json",
    }


def test_evidence_manifest_rejects_root_outside_v2_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    outside = Path("outside")
    outside.mkdir()

    with pytest.raises(ValueError, match="must stay under"):
        build_evidence_manifest(outside)


def test_evidence_manifest_accepts_configured_non_default_artifact_root(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "runtime-evidence"
    selected = artifact_root / "primary-comparison"
    selected.mkdir(parents=True)
    (selected / "result.json").write_text('{"ok":true}\n', encoding="utf-8")

    result = build_evidence_manifest(selected, artifact_root=artifact_root)

    assert result["file_count"] == 1
