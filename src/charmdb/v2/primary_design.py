from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypedDict

from charmdb.v2.protocol import PRIMARY_SEARCH_METHODS, load_manifest

PRIMARY_MANIFEST = Path("v2/config/primary-comparison.json")
CONTROL_POSITIONS = (1, 34, 66, 99, 131)
BO_METHODS = PRIMARY_SEARCH_METHODS[2:]


class _ScheduleItem(TypedDict):
    evaluation_role: str
    method: str
    budget_position: int | None
    shared_with_methods: tuple[str, ...]


@dataclass(frozen=True)
class PrimaryScheduleEntry:
    global_position: int
    seed_index: int
    seed: int
    within_seed_position: int
    evaluation_role: str
    method: str
    budget_position: int | None
    shared_with_methods: tuple[str, ...]


def _round_seed(schedule_seed: int, seed: int, phase: str, round_index: int) -> int:
    label = f"{schedule_seed}:{seed}:{phase}:{round_index}".encode()
    return int.from_bytes(hashlib.sha256(label).digest()[:8], "big")


def _candidate_schedule(seed: int, schedule_seed: int) -> list[_ScheduleItem]:
    candidates: list[_ScheduleItem] = []
    for budget_position in range(1, 13):
        round_entries: list[_ScheduleItem] = [
            {
                "evaluation_role": "CANDIDATE",
                "method": "random",
                "budget_position": budget_position,
                "shared_with_methods": (),
            },
            {
                "evaluation_role": "CANDIDATE",
                "method": "sobol",
                "budget_position": budget_position,
                "shared_with_methods": (),
            },
            {
                "evaluation_role": "BO_SHARED_INITIAL",
                "method": "bo_shared_initial",
                "budget_position": budget_position,
                "shared_with_methods": tuple(BO_METHODS),
            },
        ]
        random.Random(_round_seed(schedule_seed, seed, "initial", budget_position)).shuffle(
            round_entries
        )
        candidates.extend(round_entries)
    for budget_position in range(13, 31):
        round_entries = [
            {
                "evaluation_role": "CANDIDATE",
                "method": method,
                "budget_position": budget_position,
                "shared_with_methods": (),
            }
            for method in PRIMARY_SEARCH_METHODS
        ]
        random.Random(_round_seed(schedule_seed, seed, "adaptive", budget_position)).shuffle(
            round_entries
        )
        candidates.extend(round_entries)
    return candidates


def build_wave_a_schedule(path: Path = PRIMARY_MANIFEST) -> tuple[PrimaryScheduleEntry, ...]:
    manifest = load_manifest(path)
    if manifest.stage != "primary-comparison" or manifest.evidence_role != "PRIMARY":
        raise ValueError("primary schedule requires the dedicated PRIMARY manifest")
    payload = manifest.payload
    schedule_seed = int(payload["execution_schedule"]["schedule_seed"])
    entries: list[PrimaryScheduleEntry] = []
    global_position = 0
    for seed_index, seed in enumerate(payload["wave_a"]["seeds"], start=1):
        candidates = _candidate_schedule(int(seed), schedule_seed)
        candidate_index = 0
        for within_seed_position in range(1, 132):
            global_position += 1
            if within_seed_position in CONTROL_POSITIONS:
                entry: _ScheduleItem = {
                    "evaluation_role": "DEFAULT_CONTROL",
                    "method": "postgresql_default",
                    "budget_position": None,
                    "shared_with_methods": (),
                }
            else:
                entry = candidates[candidate_index]
                candidate_index += 1
            entries.append(
                PrimaryScheduleEntry(
                    global_position=global_position,
                    seed_index=seed_index,
                    seed=int(seed),
                    within_seed_position=within_seed_position,
                    evaluation_role=str(entry["evaluation_role"]),
                    method=str(entry["method"]),
                    budget_position=entry["budget_position"],
                    shared_with_methods=tuple(entry["shared_with_methods"]),
                )
            )
        if candidate_index != 126:
            raise RuntimeError("primary seed schedule did not consume 126 candidate observations")
    return tuple(entries)


def schedule_sha256(entries: tuple[PrimaryScheduleEntry, ...]) -> str:
    payload = [asdict(entry) for entry in entries]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def primary_design_summary(path: Path = PRIMARY_MANIFEST) -> dict[str, object]:
    manifest = load_manifest(path)
    entries = build_wave_a_schedule(path)
    counts = Counter(entry.method for entry in entries)
    digest = schedule_sha256(entries)
    frozen_digest = manifest.payload["execution_schedule"].get("schedule_sha256")
    if frozen_digest is not None and digest != frozen_digest:
        raise ValueError("generated Wave A schedule differs from its frozen SHA-256")
    return {
        "status": manifest.status,
        "execution_ready": manifest.payload["execution_ready"],
        "methods": list(PRIMARY_SEARCH_METHODS),
        "parameter_order": manifest.payload["search_space"]["parameter_order"],
        "candidate_budget_per_method": manifest.payload["candidate_budget_per_method"],
        "seeds": manifest.payload["wave_a"]["seeds"],
        "physical_observations": len(entries),
        "method_physical_observations": dict(sorted(counts.items())),
        "schedule_sha256": digest,
        "frozen_schedule_sha256": frozen_digest,
        "wave_b_status": manifest.payload["wave_b"]["status"],
        "unresolved_decisions": manifest.payload["unresolved_decisions"],
    }
