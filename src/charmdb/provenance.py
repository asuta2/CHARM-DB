from __future__ import annotations

import hashlib
from pathlib import Path

PROVENANCE_FILES = ("pyproject.toml", "uv.lock", "docker-compose.yml")
PROVENANCE_TREES = ("src/charmdb", "migrations")


def source_tree_sha256(root: Path | None = None) -> str:
    base = (root or Path.cwd()).resolve()
    paths = [base / name for name in PROVENANCE_FILES]
    for tree in PROVENANCE_TREES:
        paths.extend(path for path in (base / tree).rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(base).as_posix()):
        relative = path.relative_to(base).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
