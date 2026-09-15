import json
import runpy
import zipfile
from pathlib import Path

import pytest

PACKAGE = runpy.run_path(
    str(Path(__file__).resolve().parents[3] / "scripts/package_thesis_release.py")
)


def test_archive_verifier_rejects_changed_payload(tmp_path: Path) -> None:
    archive = tmp_path / "changed.zip"
    manifest = {"files": [{"path": "data.txt", "bytes": 4, "sha256": "0" * 64}]}
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("data.txt", "oops")
        stream.writestr("RELEASE-MANIFEST.json", json.dumps(manifest))
    with pytest.raises(ValueError, match="hash mismatch"):
        PACKAGE["verify_archive"](archive)


def test_archive_verifier_rejects_unlisted_members(tmp_path: Path) -> None:
    archive = tmp_path / "extra.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("extra.txt", "unlisted")
        stream.writestr("RELEASE-MANIFEST.json", json.dumps({"files": []}))
    with pytest.raises(ValueError, match="inventory mismatch"):
        PACKAGE["verify_archive"](archive)


def test_packaging_cannot_write_into_source_tree(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside the input root"):
        PACKAGE["build"](tmp_path, tmp_path / "archive.zip", "submission")
