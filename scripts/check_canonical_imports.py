"""Reject executable imports of retired CHARM-DB modules."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RETIRED = frozenset(
    {
        "api",
        "arm_restore",
        "calibration",
        "coordination",
        "costing",
        "drift",
        "experiment_execution",
        "experiments",
        "fidelity",
        "fingerprinting",
        "gating",
        "indexing",
        "manifest_builder",
        "manifest_preflight",
        "multiobjective",
        "observability",
        "operations",
        "optimizer",
        "reference_block",
        "search_execution",
        "soak",
        "transfer",
        "v2",
    }
)


def retired(name: str) -> bool:
    parts = name.split(".")
    return len(parts) >= 2 and parts[0] == "charmdb" and parts[1] in RETIRED


def violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
            if node.module == "charmdb":
                names.extend(f"charmdb.{alias.name}" for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and node.args
        ):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names = [first.value]
        found.extend(f"{node.lineno}: {name}" for name in names if retired(name))
    return found


def main() -> int:
    failures = []
    for directory in (ROOT / "src/charmdb", ROOT / "tests", ROOT / "scripts"):
        for path in sorted(directory.rglob("*.py")):
            failures.extend(f"{path.relative_to(ROOT)}:{item}" for item in violations(path))
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("Canonical import graph contains no retired modules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
