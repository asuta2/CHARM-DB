from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

EVIDENCE_ROOT = Path("artifacts-newpc/v2")
DEFAULT_MANIFEST_NAME = "evidence-sha256-manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _encoding_marker(path: Path) -> str | None:
    if path.suffix.lower() not in {".csv", ".json", ".log", ".md", ".txt"}:
        return None
    with path.open("rb") as handle:
        prefix = handle.read(4)
    if prefix.startswith(b"\xff\xfe"):
        return "utf-16le-bom"
    if prefix.startswith(b"\xfe\xff"):
        return "utf-16be-bom"
    if prefix.startswith(b"\xef\xbb\xbf"):
        return "utf-8-bom"
    return "no-bom"


def build_evidence_manifest(
    root: Path = EVIDENCE_ROOT,
    output_path: Path | None = None,
    *,
    artifact_root: Path = EVIDENCE_ROOT,
) -> dict[str, Any]:
    allowed_root = artifact_root.resolve()
    resolved_root = root.resolve()
    if resolved_root != allowed_root and allowed_root not in resolved_root.parents:
        raise ValueError(f"evidence root must stay under artifact directory {allowed_root}")
    if not resolved_root.is_dir():
        raise ValueError(f"evidence root does not exist: {resolved_root}")
    output = (output_path or resolved_root / DEFAULT_MANIFEST_NAME).resolve()
    if output != resolved_root and resolved_root not in output.parents:
        raise ValueError("evidence manifest output must stay under the selected evidence root")

    entries: list[dict[str, Any]] = []
    for path in sorted(resolved_root.rglob("*"), key=lambda item: item.as_posix()):
        if path.resolve() == output or path.resolve() == output.with_name(f"{output.name}.tmp"):
            continue
        if path.is_symlink():
            raise ValueError(f"evidence tree contains unsupported symbolic link: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(resolved_root).as_posix()
        entries.append(
            {
                "relative_path": relative,
                "byte_size": path.stat().st_size,
                "sha256": _sha256(path),
                "encoding_marker": _encoding_marker(path),
            }
        )
    core = {
        "schema_version": 1,
        "root": root.as_posix(),
        "file_count": len(entries),
        "total_bytes": sum(item["byte_size"] for item in entries),
        "files": entries,
    }
    canonical = json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload = {**core, "manifest_payload_sha256": hashlib.sha256(canonical).hexdigest()}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    return {
        "output_path": str(output),
        "file_sha256": _sha256(output),
        "manifest_payload_sha256": payload["manifest_payload_sha256"],
        "file_count": payload["file_count"],
        "total_bytes": payload["total_bytes"],
        "non_utf8_bom_files": [
            item["relative_path"]
            for item in entries
            if item["encoding_marker"] in {"utf-16le-bom", "utf-16be-bom"}
        ],
    }
