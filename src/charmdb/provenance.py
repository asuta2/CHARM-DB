from __future__ import annotations

import hashlib
from pathlib import Path

PROVENANCE_FILES = ("pyproject.toml", "uv.lock", "docker-compose.yml")
PROVENANCE_TREES = ("src/charmdb", "migrations")
INVENTORY_TREES_V2 = (
    *PROVENANCE_TREES,
    "experiments/thesis/manifests",
    "experiments/thesis/preregistration",
)
GENERATED_PARTS = frozenset({"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"})


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


def source_inventory_sha256_v2(root: Path | None = None) -> str:
    """Hash source and frozen inputs while excluding generated cache files.

    This is a new contract. Existing ``source_tree_sha256`` records retain their
    original meaning and must never be recalculated under this algorithm.
    """
    base = (root or Path.cwd()).resolve()
    paths = [base / name for name in PROVENANCE_FILES if (base / name).is_file()]
    for tree in INVENTORY_TREES_V2:
        paths.extend(
            path
            for path in (base / tree).rglob("*")
            if path.is_file()
            and not GENERATED_PARTS.intersection(path.relative_to(base).parts)
            and path.suffix not in {".pyc", ".pyo"}
        )
    digest = hashlib.sha256(b"charmdb-source-inventory-v2\0")
    for path in sorted(paths, key=lambda item: item.relative_to(base).as_posix()):
        digest.update(path.relative_to(base).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
