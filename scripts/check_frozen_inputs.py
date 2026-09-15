"""Verify byte-frozen thesis manifests and preregistration after repository moves."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "experiments/thesis/frozen-sha256.json"


def main() -> int:
    rows = json.loads(INVENTORY.read_text(encoding="utf-8"))
    if len(rows) != 16:
        raise ValueError(f"Expected 16 frozen inputs, found {len(rows)}")
    for row in rows:
        logical = Path(row["logical_path"])
        if logical == Path("v2/docs/wave-b-final-analysis-preregistration.md"):
            physical = ROOT / "experiments/thesis/preregistration" / logical.name
        elif logical.parts[:2] == ("v2", "config"):
            physical = ROOT / "experiments/thesis/manifests" / logical.name
        else:
            raise ValueError(f"Unexpected frozen logical path: {logical}")
        data = physical.read_bytes()
        if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError(f"Frozen input changed: {logical}")
    print("All 16 frozen thesis inputs match byte size and SHA-256")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
