from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, TypedDict

import torch

from charmdb.v2.protocol import FINAL_SCREENING_PARAMETERS, PRIMARY_SEARCH_METHODS, load_manifest

PRIMARY_MANIFEST = Path("v2/config/primary-comparison.json")
CONTROL_POSITIONS = (1, 34, 66, 99, 131)
BO_METHODS = PRIMARY_SEARCH_METHODS[2:]
PRIMARY_PARAMETER_ORDER = FINAL_SCREENING_PARAMETERS


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


@dataclass(frozen=True)
class PrimaryCandidate:
    vector: tuple[float, ...]
    configuration: dict[str, str]


@dataclass(frozen=True)
class PrimaryPlanEntry:
    schedule: PrimaryScheduleEntry
    candidate: PrimaryCandidate | None


def _round_seed(schedule_seed: int, seed: int, phase: str, round_index: int) -> int:
    label = f"{schedule_seed}:{seed}:{phase}:{round_index}".encode()
    return int.from_bytes(hashlib.sha256(label).digest()[:8], "big")


def _derived_seed(label: str) -> int:
    return int.from_bytes(hashlib.sha256(label.encode()).digest()[:4], "big") % 2_147_483_647


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def decode_primary_point(
    normalized: list[float] | tuple[float, ...], payload: dict[str, Any]
) -> PrimaryCandidate:
    order = list(payload["search_space"]["parameter_order"])
    if order != list(PRIMARY_PARAMETER_ORDER) or len(normalized) != len(order):
        raise ValueError("primary point dimension differs from the frozen parameter order")
    parameters = dict(payload["search_space"]["parameters"])
    configuration: dict[str, str] = {}
    vector = tuple(float(item) for item in normalized)
    for name, raw_fraction in zip(order, vector, strict=True):
        if not 0.0 <= raw_fraction <= 1.0:
            raise ValueError("normalized primary coordinates must be in [0,1]")
        specification = dict(parameters[name])
        lower = Decimal(str(specification["lower"]))
        upper = Decimal(str(specification["upper"]))
        fraction = Decimal(str(raw_fraction))
        if specification["type"] == "integer":
            cardinality = int(upper - lower) + 1
            offset = min(
                int((fraction * cardinality).to_integral_value(rounding=ROUND_FLOOR)),
                cardinality - 1,
            )
            configuration[name] = str(int(lower) + offset)
        elif specification["type"] == "continuous":
            value = (lower + fraction * (upper - lower)).quantize(Decimal("0.000001"))
            configuration[name] = _decimal_text(value)
        else:
            raise ValueError(f"unsupported primary parameter type for {name}")
    return PrimaryCandidate(vector=vector, configuration=configuration)


def encode_primary_configuration(
    configuration: dict[str, str], payload: dict[str, Any]
) -> tuple[float, ...]:
    order = list(payload["search_space"]["parameter_order"])
    if order != list(PRIMARY_PARAMETER_ORDER) or set(configuration) != set(order):
        raise ValueError("primary configuration differs from the frozen parameter order")
    parameters = dict(payload["search_space"]["parameters"])
    encoded: list[float] = []
    for name in order:
        specification = dict(parameters[name])
        lower = Decimal(str(specification["lower"]))
        upper = Decimal(str(specification["upper"]))
        value = Decimal(str(configuration[name]))
        if not lower <= value <= upper:
            raise ValueError(f"primary configuration value for {name} is outside its bounds")
        if specification["type"] == "integer":
            if value != value.to_integral_value():
                raise ValueError(f"primary integer parameter {name} is not integral")
            cardinality = int(upper - lower) + 1
            fraction = (value - lower + Decimal("0.5")) / Decimal(cardinality)
        elif specification["type"] == "continuous":
            fraction = (value - lower) / (upper - lower)
        else:
            raise ValueError(f"unsupported primary parameter type for {name}")
        encoded.append(float(fraction))
    return tuple(encoded)


def _unique_candidates(
    points: list[list[float]], payload: dict[str, Any], count: int
) -> tuple[PrimaryCandidate, ...]:
    candidates: list[PrimaryCandidate] = []
    seen: set[str] = set()
    for point in points:
        candidate = decode_primary_point(point, payload)
        key = json.dumps(candidate.configuration, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            candidates.append(candidate)
            seen.add(key)
        if len(candidates) == count:
            return tuple(candidates)
    raise RuntimeError("primary design did not produce enough unique configurations")


def primary_method_design(
    seed: int, method: str, payload: dict[str, Any]
) -> tuple[PrimaryCandidate, ...]:
    if method == "random":
        count = int(payload["candidate_budget_per_method"])
        generator = random.Random(_derived_seed(f"thesis-protocol-v2/primary/{seed}/random-design"))
        points = [[generator.random() for _ in PRIMARY_PARAMETER_ORDER] for _ in range(count * 2)]
    elif method == "sobol":
        count = int(payload["candidate_budget_per_method"])
        engine = torch.quasirandom.SobolEngine(  # type: ignore[no-untyped-call]
            dimension=len(PRIMARY_PARAMETER_ORDER),
            scramble=True,
            seed=_derived_seed(f"thesis-protocol-v2/primary/{seed}/sobol-design"),
        )
        points = engine.draw(count * 2, dtype=torch.float64).tolist()
    elif method == "bo_shared_initial":
        count = int(payload["shared_bo_initial_design"]["size"])
        engine = torch.quasirandom.SobolEngine(  # type: ignore[no-untyped-call]
            dimension=len(PRIMARY_PARAMETER_ORDER),
            scramble=True,
            seed=_derived_seed(f"thesis-protocol-v2/primary/{seed}/bo-shared-initial"),
        )
        points = engine.draw(count * 2, dtype=torch.float64).tolist()
    else:
        raise ValueError(f"unsupported fixed primary design method {method!r}")
    return _unique_candidates(points, payload, count)


def primary_candidate_design_payload(payload: dict[str, Any]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for seed in payload["wave_a"]["seeds"]:
        for method in ("random", "sobol", "bo_shared_initial"):
            for position, candidate in enumerate(
                primary_method_design(int(seed), method, payload), start=1
            ):
                rows.append(
                    {
                        "seed": int(seed),
                        "method": method,
                        "budget_position": position,
                        "vector": list(candidate.vector),
                        "configuration": candidate.configuration,
                    }
                )
    return rows


def primary_candidate_design_sha256(payload: dict[str, Any]) -> str:
    return _canonical_sha256(primary_candidate_design_payload(payload))


def primary_restore_soak_contract_sha256(payload: dict[str, Any]) -> str:
    """Hash the frozen inputs that make restore-soak evidence reusable.

    Full manifest bytes are intentionally excluded because P007 changes
    authorization metadata after the soak without changing this contract.
    """
    environment = dict(payload["pre_campaign_environment_gates"])
    profile = dict(payload["benchmark_profile"])
    contract = {
        "protocol_id": payload["protocol_id"],
        "stage": payload["stage"],
        "benchmark_profile": profile,
        "minimum_consecutive_restore_passes": environment["minimum_consecutive_restore_passes"],
        "restore_mechanism": profile["restore_mechanism"],
        "candidate_restore_before_every_physical_observation": profile[
            "candidate_restore_before_every_physical_observation"
        ],
    }
    return _canonical_sha256(contract)


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


def build_wave_a_plan(path: Path = PRIMARY_MANIFEST) -> tuple[PrimaryPlanEntry, ...]:
    manifest = load_manifest(path)
    payload = manifest.payload
    designs = {
        (int(seed), method): primary_method_design(int(seed), method, payload)
        for seed in payload["wave_a"]["seeds"]
        for method in ("random", "sobol", "bo_shared_initial")
    }
    plan: list[PrimaryPlanEntry] = []
    for entry in build_wave_a_schedule(path):
        candidate: PrimaryCandidate | None = None
        if entry.method in {"random", "sobol", "bo_shared_initial"}:
            if entry.budget_position is None:
                raise RuntimeError("fixed primary candidate is missing its budget position")
            candidate = designs[(entry.seed, entry.method)][entry.budget_position - 1]
        plan.append(PrimaryPlanEntry(schedule=entry, candidate=candidate))
    return tuple(plan)


def export_primary_design(
    manifest_path: Path = PRIMARY_MANIFEST,
    output_path: Path | None = None,
) -> dict[str, object]:
    manifest = load_manifest(manifest_path)
    plan = build_wave_a_plan(manifest_path)
    output = output_path or Path(manifest.artifact_root) / "primary-design.json"
    artifact_root = Path(manifest.artifact_root).resolve()
    resolved_output = output.resolve()
    if resolved_output != artifact_root and artifact_root not in resolved_output.parents:
        raise ValueError("primary design export must stay under the manifest artifact root")
    payload = {
        "protocol_id": manifest.payload["protocol_id"],
        "stage": manifest.stage,
        "schedule_sha256": schedule_sha256(tuple(item.schedule for item in plan)),
        "candidate_design_sha256": primary_candidate_design_sha256(manifest.payload),
        "entries": [
            {
                **asdict(item.schedule),
                "candidate_vector": list(item.candidate.vector) if item.candidate else None,
                "requested_configuration": (
                    item.candidate.configuration if item.candidate else None
                ),
            }
            for item in plan
        ],
    }
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {
        "output_path": str(resolved_output),
        "sha256": hashlib.sha256(resolved_output.read_bytes()).hexdigest(),
        "schedule_sha256": payload["schedule_sha256"],
        "candidate_design_sha256": payload["candidate_design_sha256"],
    }


def primary_design_summary(path: Path = PRIMARY_MANIFEST) -> dict[str, object]:
    manifest = load_manifest(path)
    entries = build_wave_a_schedule(path)
    counts = Counter(entry.method for entry in entries)
    digest = schedule_sha256(entries)
    frozen_digest = manifest.payload["execution_schedule"].get("schedule_sha256")
    if frozen_digest is not None and digest != frozen_digest:
        raise ValueError("generated Wave A schedule differs from its frozen SHA-256")
    candidate_digest = primary_candidate_design_sha256(manifest.payload)
    frozen_candidate_digest = manifest.payload["shared_bo_initial_design"].get(
        "candidate_design_sha256"
    )
    if frozen_candidate_digest is not None and candidate_digest != frozen_candidate_digest:
        raise ValueError("generated primary candidate design differs from its frozen SHA-256")
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
        "candidate_design_sha256": candidate_digest,
        "frozen_candidate_design_sha256": frozen_candidate_digest,
        "wave_b_status": manifest.payload["wave_b"]["status"],
        "unresolved_decisions": manifest.payload["unresolved_decisions"],
    }
