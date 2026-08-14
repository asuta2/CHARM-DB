from __future__ import annotations

import hashlib
import json
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

import torch

from charmdb.v2.protocol import load_manifest

SCREENING_MANIFEST = Path("v2/config/parameter-screening.json")
SCREENING_STAGE = "parameter-screening"
PARAMETER_ORDER = (
    "shared_buffers",
    "effective_cache_size",
    "work_mem",
    "maintenance_work_mem",
    "checkpoint_timeout",
    "checkpoint_completion_target",
    "max_wal_size",
    "random_page_cost",
    "effective_io_concurrency",
    "default_statistics_target",
    "max_parallel_workers_per_gather",
)
EXPECTED_PARAMETERS: dict[str, dict[str, object]] = {
    "shared_buffers": {"type": "integer", "lower": 16384, "upper": 183500},
    "effective_cache_size": {"type": "integer", "lower": 131072, "upper": 524288},
    "work_mem": {"type": "integer", "lower": 1024, "upper": 32768},
    "maintenance_work_mem": {"type": "integer", "lower": 32768, "upper": 524288},
    "checkpoint_timeout": {"type": "integer", "lower": 300, "upper": 1800},
    "checkpoint_completion_target": {"type": "continuous", "lower": 0.7, "upper": 0.95},
    "max_wal_size": {"type": "integer", "lower": 512, "upper": 4096},
    "random_page_cost": {"type": "continuous", "lower": 1.0, "upper": 4.0},
    "effective_io_concurrency": {"type": "integer", "lower": 0, "upper": 200},
    "default_statistics_target": {"type": "integer", "lower": 100, "upper": 500},
    "max_parallel_workers_per_gather": {"type": "integer", "lower": 0, "upper": 4},
}
EXPECTED_DEFAULT_CONFIGURATION = {
    "shared_buffers": "16384",
    "effective_cache_size": "524288",
    "work_mem": "4096",
    "maintenance_work_mem": "65536",
    "checkpoint_timeout": "300",
    "checkpoint_completion_target": "0.9",
    "max_wal_size": "1024",
    "random_page_cost": "4",
    "effective_io_concurrency": "16",
    "default_statistics_target": "100",
    "max_parallel_workers_per_gather": "2",
}
RESERVED_SEEDS = frozenset(
    {
        20260731,
        20260801,
        20260802,
        88408573,
        1418705027,
        642754166,
        1902413987,
        740267717,
        845196880,
        1447054499,
        1896818828,
        1369360384,
        1784014326,
    }
)


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _derived_seed(label: str) -> int:
    return int.from_bytes(hashlib.sha256(label.encode()).digest()[:4], "big") % 2_147_483_647


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def decode_screening_point(
    normalized: list[float],
    payload: dict[str, Any],
) -> dict[str, str]:
    order = list(payload["design"]["parameter_order"])
    if order != list(PARAMETER_ORDER) or len(normalized) != len(order):
        raise ValueError("screening point dimension differs from the frozen parameter order")
    parameters = dict(payload["search_space"]["parameters"])
    configuration: dict[str, str] = {}
    for name, raw_fraction in zip(order, normalized, strict=True):
        if not 0.0 <= raw_fraction < 1.0:
            raise ValueError("normalized Sobol coordinates must be in [0,1)")
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
            raise ValueError(f"unsupported screening parameter type for {name}")
    return configuration


def screening_sobol_configurations(payload: dict[str, Any]) -> list[dict[str, object]]:
    design = dict(payload["design"])
    count = int(design["joint_sobol_configurations"])
    seed = int(design["screening_seed"])
    engine = torch.quasirandom.SobolEngine(  # type: ignore[no-untyped-call]
        dimension=len(PARAMETER_ORDER),
        scramble=True,
        seed=seed,
    )
    points = engine.draw(count, dtype=torch.float64).tolist()
    return [
        {
            "sobol_index": index,
            "configuration": decode_screening_point(point, payload),
        }
        for index, point in enumerate(points, start=1)
    ]


def screening_initial_schedule(payload: dict[str, Any]) -> list[dict[str, object]]:
    design = dict(payload["design"])
    default_positions = set(design["default_control_positions"])
    total = int(design["joint_sobol_configurations"]) + int(
        design["interleaved_default_controls"]
    )
    defaults = {
        str(name): str(value) for name, value in dict(payload["default_configuration"]).items()
    }
    sobol = iter(screening_sobol_configurations(payload))
    schedule: list[dict[str, object]] = []
    for position in range(1, total + 1):
        if position in default_positions:
            schedule.append(
                {
                    "position": position,
                    "kind": "DEFAULT_CONTROL",
                    "configuration": defaults,
                }
            )
            continue
        point = next(sobol)
        schedule.append(
            {
                "position": position,
                "kind": "SOBOL",
                **point,
            }
        )
    try:
        next(sobol)
    except StopIteration:
        return schedule
    raise ValueError("screening schedule did not consume the frozen Sobol design")


def screening_manifest_payload(
    path: Path = SCREENING_MANIFEST,
) -> tuple[str, dict[str, Any]]:
    manifest = load_manifest(path)
    if manifest.stage != SCREENING_STAGE or manifest.evidence_role != "CALIBRATION":
        raise ValueError("screening design requires its dedicated CALIBRATION manifest")
    payload = manifest.payload
    design = dict(payload["design"])
    seed_derivation = dict(design["seed_derivation"])
    label = str(seed_derivation["label"])
    seed = int(design["screening_seed"])
    if seed_derivation.get("algorithm") != "sha256-first-32-bits-mod-2147483647":
        raise ValueError("unsupported screening-seed derivation")
    if seed != _derived_seed(label) or seed in RESERVED_SEEDS:
        raise ValueError("screening seed is not the fresh reproducible seed")
    if list(design["parameter_order"]) != list(PARAMETER_ORDER):
        raise ValueError("screening parameter order differs from the frozen order")
    if payload["search_space"]["parameters"] != EXPECTED_PARAMETERS:
        raise ValueError("screening bounds differ from the frozen 11-knob pool")
    if payload.get("default_configuration") != EXPECTED_DEFAULT_CONFIGURATION:
        raise ValueError("screening default differs from the frozen PostgreSQL-default vector")
    expected_generator = {
        "implementation": "torch.quasirandom.SobolEngine",
        "verified_torch_version": "2.13.0+cpu",
        "scramble": True,
        "draw_dtype": "float64",
        "integer_decode": "lower+floor(u*(upper-lower+1)), clamped to upper",
        "continuous_decimal_places": 6,
    }
    if design.get("generator") != expected_generator:
        raise ValueError("screening generator differs from the frozen implementation")
    if design.get("default_control_positions") != [1, 18, 35]:
        raise ValueError("screening defaults must remain at positions 1, 18, and 35")
    configurations = screening_sobol_configurations(payload)
    schedule = screening_initial_schedule(payload)
    if _canonical_sha256(configurations) != design.get("sobol_design_sha256"):
        raise ValueError("generated Sobol configurations differ from the frozen design hash")
    if _canonical_sha256(schedule) != design.get("initial_schedule_sha256"):
        raise ValueError("generated screening schedule differs from the frozen schedule hash")
    return hashlib.sha256(path.read_bytes()).hexdigest(), payload


def screening_design_summary(path: Path = SCREENING_MANIFEST) -> dict[str, object]:
    manifest_sha256, payload = screening_manifest_payload(path)
    design = dict(payload["design"])
    runtime = dict(payload["runtime"])
    return {
        "status": payload["status"],
        "execution_ready": payload["execution_ready"],
        "manifest_sha256": manifest_sha256,
        "screening_seed": design["screening_seed"],
        "sobol_design_sha256": design["sobol_design_sha256"],
        "initial_schedule_sha256": design["initial_schedule_sha256"],
        "initial_observations": 35,
        "maximum_observations": 41,
        "initial_restore_inclusive_hours": runtime["initial_restore_inclusive_hours"],
        "maximum_restore_inclusive_hours": runtime["maximum_restore_inclusive_hours"],
        "unresolved_decisions": payload["unresolved_decisions"],
    }


def export_screening_design(
    path: Path = SCREENING_MANIFEST,
    output: Path | None = None,
) -> dict[str, str]:
    manifest_sha256, payload = screening_manifest_payload(path)
    artifact_root = Path(str(payload["artifact_root"])).resolve()
    destination = (
        output.resolve()
        if output is not None
        else (artifact_root / "parameter-screening-design.json").resolve()
    )
    try:
        destination.relative_to(artifact_root)
    except ValueError as error:
        raise ValueError("screening design export must stay under its artifact root") from error
    export = {
        "manifest_sha256": manifest_sha256,
        "sobol_design_sha256": payload["design"]["sobol_design_sha256"],
        "initial_schedule_sha256": payload["design"]["initial_schedule_sha256"],
        "schedule": screening_initial_schedule(payload),
        "analysis_plan": payload["analysis_plan"],
        "runtime": payload["runtime"],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(export, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return {
        "manifest_sha256": manifest_sha256,
        "design_sha256": _canonical_sha256(export),
        "output_path": str(destination),
    }
