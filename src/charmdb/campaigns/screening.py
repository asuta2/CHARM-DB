from __future__ import annotations

import hashlib
import json
import math
import statistics
import uuid
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, cast

import numpy as np
import torch
from psycopg.types.json import Jsonb

from charmdb.campaigns.default_reference import (
    FROZEN_BASELINE_ID,
    FROZEN_CLIENT_THREADS,
    FROZEN_CONCURRENCY,
    FROZEN_MAINTENANCE_POLICY,
    FROZEN_MEASUREMENT_SECONDS,
    FROZEN_PREFLIGHT_ID,
    FROZEN_PROFILE_ID,
    FROZEN_RESTORE_MECHANISM,
    FROZEN_WARMUP_SECONDS,
)
from charmdb.config import Settings
from charmdb.controller import discover_knobs, validate_candidate
from charmdb.db import connect
from charmdb.protocol import load_manifest
from charmdb.restore.preflight import _target_safety
from charmdb.worker import (
    control_campaign,
    create_campaign,
    create_v2_tuned_benchmark_trial,
    run_once,
)

SCREENING_MANIFEST = Path("experiments/thesis/manifests/parameter-screening.json")
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


@dataclass(frozen=True)
class ScreeningStep:
    action: str
    campaign_id: uuid.UUID
    block_id: uuid.UUID
    run_id: uuid.UUID | None
    trial_id: uuid.UUID | None
    details: dict[str, Any]


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


def screening_plan_rows(
    payload: dict[str, Any],
    block_id: uuid.UUID,
    campaign_id: uuid.UUID,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in screening_initial_schedule(payload):
        sequence = int(str(item["position"]))
        kind = str(item["kind"])
        rows.append(
            {
                "run_id": uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"charmdb:{block_id}:initial:{sequence}:{kind}",
                ),
                "block_id": block_id,
                "campaign_id": campaign_id,
                "sequence": sequence,
                "chronological_execution_index": sequence,
                "phase": "INITIAL",
                "evaluation_kind": kind,
                "sobol_index": item.get("sobol_index"),
                "oat_parameter": None,
                "random_seed": int(payload["design"]["screening_seed"]),
                "requested_configuration": item["configuration"],
                "status": "PLANNED",
            }
        )
    return rows


def screening_oat_plan_rows(
    payload: dict[str, Any],
    block_id: uuid.UUID,
    campaign_id: uuid.UUID,
    oat_parameters: list[str],
) -> list[dict[str, object]]:
    if (
        len(oat_parameters) > 3
        or len(set(oat_parameters)) != len(oat_parameters)
        or any(name not in PARAMETER_ORDER for name in oat_parameters)
    ):
        raise ValueError("screening OAT plan requires at most three unique frozen knobs")
    rows: list[dict[str, object]] = []
    for pair_index, name in enumerate(oat_parameters):
        for endpoint_index, (kind, endpoint) in enumerate(
            (("OAT_LOW", "lower"), ("OAT_HIGH", "upper"))
        ):
            sequence = 36 + pair_index * 2 + endpoint_index
            rows.append(
                {
                    "run_id": uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"charmdb:{block_id}:oat:{sequence}:{name}:{kind}",
                    ),
                    "block_id": block_id,
                    "campaign_id": campaign_id,
                    "sequence": sequence,
                    "chronological_execution_index": sequence,
                    "phase": "OAT",
                    "evaluation_kind": kind,
                    "sobol_index": None,
                    "oat_parameter": name,
                    "random_seed": int(payload["design"]["screening_seed"]),
                    "requested_configuration": _oat_configuration(payload, name, endpoint),
                    "status": "PLANNED",
                }
            )
    return rows


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


def _average_ranks(values: list[float]) -> np.ndarray[Any, np.dtype[np.float64]]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = np.empty(len(values), dtype=np.float64)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        average = ((start + 1) + end) / 2
        for position in range(start, end):
            ranks[order[position]] = average
        start = end
    return ranks


def _residuals(
    target: np.ndarray[Any, np.dtype[np.float64]],
    controls: np.ndarray[Any, np.dtype[np.float64]],
) -> np.ndarray[Any, np.dtype[np.float64]]:
    design = np.column_stack((np.ones(len(target), dtype=np.float64), controls))
    coefficients = np.linalg.lstsq(design, target, rcond=None)[0]
    return cast(np.ndarray[Any, np.dtype[np.float64]], target - design @ coefficients)


def _correlation(
    left: np.ndarray[Any, np.dtype[np.float64]],
    right: np.ndarray[Any, np.dtype[np.float64]],
) -> float:
    centered_left = left - left.mean()
    centered_right = right - right.mean()
    denominator = math.sqrt(
        float(centered_left @ centered_left) * float(centered_right @ centered_right)
    )
    if denominator <= 1e-12:
        return 0.0
    return max(-1.0, min(1.0, float(centered_left @ centered_right) / denominator))


def partial_rank_correlations(
    configurations: list[dict[str, str]],
    response: list[float],
) -> dict[str, float]:
    if len(configurations) != len(response) or len(configurations) < len(PARAMETER_ORDER) + 3:
        raise ValueError("PRCC requires matched observations and residual degrees of freedom")
    ranked_inputs = np.column_stack(
        [
            _average_ranks([float(configuration[name]) for configuration in configurations])
            for name in PARAMETER_ORDER
        ]
    )
    ranked_response = _average_ranks(response)
    correlations: dict[str, float] = {}
    for index, name in enumerate(PARAMETER_ORDER):
        controls = np.delete(ranked_inputs, index, axis=1)
        correlations[name] = _correlation(
            _residuals(ranked_inputs[:, index], controls),
            _residuals(ranked_response, controls),
        )
    return correlations


def _valid_measurement(point: dict[str, Any]) -> bool:
    try:
        tps = float(point["throughput_tps"])
        p99 = float(point["p99_ms"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        point.get("status") == "COMPLETED"
        and math.isfinite(tps)
        and math.isfinite(p99)
        and tps > 0
        and p99 > 0
        and int(point.get("failures", 0)) == 0
        and point.get("exact_core_passed") is True
        and point.get("physical_statistics_passed") is True
    )


def _fitted_change(values: list[float], positions: list[int]) -> float:
    mean_x = statistics.mean(positions)
    mean_y = statistics.mean(values)
    denominator = sum((position - mean_x) ** 2 for position in positions)
    slope = sum(
        (position - mean_x) * (value - mean_y)
        for position, value in zip(positions, values, strict=True)
    ) / denominator
    return abs(slope) * (max(positions) - min(positions))


def _selection_from_scores(
    scores: dict[str, float],
    threshold: float,
    *,
    oat_inclusion: dict[str, bool] | None = None,
    unsafe: set[str] | None = None,
) -> dict[str, object]:
    oat_inclusion = oat_inclusion or {}
    unsafe = unsafe or set()
    included = {name for name, score in scores.items() if score >= threshold}
    for name, include in oat_inclusion.items():
        if include:
            included.add(name)
        else:
            included.discard(name)
    included.difference_update(unsafe)
    required_unsafe = "shared_buffers" in unsafe
    included.add("shared_buffers")
    if len(included) < 8:
        eligible = sorted(
            (name for name in PARAMETER_ORDER if name not in included and name not in unsafe),
            key=lambda name: (-scores[name], PARAMETER_ORDER.index(name)),
        )
        included.update(eligible[: 8 - len(included)])
    ordered_included = [name for name in PARAMETER_ORDER if name in included]
    return {
        "included": ordered_included,
        "excluded": [name for name in PARAMETER_ORDER if name not in included],
        "dimension": len(ordered_included),
        "required_parameter_unsafe": required_unsafe,
        "passed": not required_unsafe and 8 <= len(ordered_included) <= 12,
    }


def analyze_screening_observations(
    points: list[dict[str, Any]],
    payload: dict[str, Any],
    *,
    initial_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    initial = sorted(
        (point for point in points if point.get("phase") == "INITIAL"),
        key=lambda point: int(point["chronological_execution_index"]),
    )
    if len(initial) != 35 or [
        int(point["chronological_execution_index"]) for point in initial
    ] != list(range(1, 36)):
        raise ValueError("screening analysis requires the complete frozen 35-slot schedule")
    controls = [point for point in initial if point.get("evaluation_kind") == "DEFAULT_CONTROL"]
    sobol = [point for point in initial if point.get("evaluation_kind") == "SOBOL"]
    if len(controls) != 3 or len(sobol) != 32:
        raise ValueError("screening schedule must contain 32 Sobol and three default slots")
    valid_controls = [point for point in controls if _valid_measurement(point)]
    valid_sobol = [point for point in sobol if _valid_measurement(point)]
    plan = dict(payload["analysis_plan"])
    minimum_valid = int(plan["valid_sobol_measurements_minimum"])
    result: dict[str, Any] = {
        "initial_observations": 35,
        "valid_sobol_measurements": len(valid_sobol),
        "invalid_sobol_measurements": len(sobol) - len(valid_sobol),
        "valid_default_controls": len(valid_controls),
    }
    control_drift: dict[str, Any]
    if len(valid_controls) == 3:
        positions = [int(point["chronological_execution_index"]) for point in valid_controls]
        control_tps = [float(point["throughput_tps"]) for point in valid_controls]
        control_p99 = [float(point["p99_ms"]) for point in valid_controls]
        tps_change = _fitted_change(control_tps, positions)
        p99_change = _fitted_change(control_p99, positions)
        tps_relative = tps_change / max(abs(statistics.mean(control_tps)), 1e-12)
        drift_plan = dict(plan["control_drift"])
        control_drift = {
            "tps_fitted_block_change": tps_change,
            "tps_fitted_block_change_relative": tps_relative,
            "p99_fitted_block_change_ms": p99_change,
            "tps_passed": tps_relative
            <= float(drift_plan["maximum_tps_fitted_block_change_relative"]),
            "p99_passed": p99_change
            <= float(drift_plan["maximum_p99_fitted_block_change_ms"]),
        }
        control_drift["passed"] = control_drift["tps_passed"] and control_drift["p99_passed"]
    else:
        control_drift = {"passed": False, "reason": "invalid default control"}
    result["control_drift"] = control_drift
    if len(valid_sobol) < minimum_valid:
        return {**result, "outcome": "BLOCKED_VALIDITY", "oat_parameters": []}
    if control_drift["passed"] is not True:
        return {**result, "outcome": "BLOCKED_DRIFT", "oat_parameters": []}
    configurations = [dict(point["requested_configuration"]) for point in valid_sobol]
    tps_prcc = partial_rank_correlations(
        configurations, [float(point["throughput_tps"]) for point in valid_sobol]
    )
    p99_prcc = partial_rank_correlations(
        configurations, [float(point["p99_ms"]) for point in valid_sobol]
    )
    scores = {
        name: max(abs(tps_prcc[name]), abs(p99_prcc[name])) for name in PARAMETER_ORDER
    }
    lower, upper = [float(value) for value in plan["ambiguity_band_inclusive"]]
    threshold = float(plan["inclusion_threshold"])
    ambiguous = [name for name in PARAMETER_ORDER if lower <= scores[name] <= upper]
    oat_parameters = sorted(
        ambiguous,
        key=lambda name: (abs(scores[name] - threshold), PARAMETER_ORDER.index(name)),
    )[:3]
    result.update(
        {
            "prcc": {
                name: {
                    "throughput_tps": tps_prcc[name],
                    "p99_ms": p99_prcc[name],
                    "maximum_absolute": scores[name],
                }
                for name in PARAMETER_ORDER
            },
            "oat_parameters": oat_parameters,
        }
    )
    if initial_analysis is None and oat_parameters:
        return {**result, "outcome": "OAT_REQUIRED"}
    if initial_analysis is None:
        selection = _selection_from_scores(scores, threshold)
        return {
            **result,
            "outcome": "PASSED" if selection["passed"] else "BLOCKED_SELECTION",
            "selection": selection,
            "oat_results": {},
        }
    frozen_oat = list(initial_analysis.get("oat_parameters") or [])
    if oat_parameters != frozen_oat:
        raise ValueError("recomputed screening ambiguity differs from persisted initial analysis")
    oat_points = [point for point in points if point.get("phase") == "OAT"]
    if len(oat_points) != len(frozen_oat) * 2:
        raise ValueError("screening final analysis requires every planned OAT endpoint")
    default_tps = statistics.median(float(point["throughput_tps"]) for point in valid_controls)
    oat_plan = dict(payload["design"]["targeted_oat"])
    oat_inclusion: dict[str, bool] = {}
    unsafe: set[str] = set()
    oat_results: dict[str, Any] = {}
    for name in frozen_oat:
        pair = {
            str(point["evaluation_kind"]): point
            for point in oat_points
            if point.get("oat_parameter") == name
        }
        low = pair.get("OAT_LOW")
        high = pair.get("OAT_HIGH")
        valid = (
            low is not None
            and high is not None
            and _valid_measurement(low)
            and _valid_measurement(high)
        )
        if not valid:
            unsafe.add(name)
            oat_inclusion[name] = False
            oat_results[name] = {"valid": False, "included": False}
            continue
        assert low is not None and high is not None
        tps_range = abs(float(high["throughput_tps"]) - float(low["throughput_tps"]))
        p99_range = abs(float(high["p99_ms"]) - float(low["p99_ms"]))
        tps_relative = tps_range / max(abs(default_tps), 1e-12)
        include = (
            tps_relative >= float(oat_plan["tps_endpoint_range_relative_threshold"])
            or p99_range >= float(oat_plan["p99_endpoint_range_ms_threshold"])
        )
        oat_inclusion[name] = include
        oat_results[name] = {
            "valid": True,
            "tps_endpoint_range": tps_range,
            "tps_endpoint_range_relative_to_default": tps_relative,
            "p99_endpoint_range_ms": p99_range,
            "included": include,
        }
    selection = _selection_from_scores(
        scores,
        threshold,
        oat_inclusion=oat_inclusion,
        unsafe=unsafe,
    )
    return {
        **result,
        "outcome": "PASSED" if selection["passed"] else "BLOCKED_SELECTION",
        "selection": selection,
        "oat_results": oat_results,
    }


def screening_readiness(
    settings: Settings,
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest_path: Path = SCREENING_MANIFEST,
) -> dict[str, Any]:
    manifest_sha256, payload = screening_manifest_payload(manifest_path)
    if preflight_id != FROZEN_PREFLIGHT_ID:
        raise ValueError("screening must use the frozen scale-500 preflight")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT preflight_id,restore_mechanism,approved
               FROM charm_control.experiment_candidate_dataset_baselines
               WHERE baseline_id=%s""",
            (FROZEN_BASELINE_ID,),
        )
        baseline = cur.fetchone()
        cur.execute(
            """SELECT b.block_id,b.status,c.status AS campaign_status
               FROM charm_control.experiment_v2_default_reference_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.status='PASSED' AND b.baseline_id=%s
                 AND b.benchmark_profile_id=%s
               ORDER BY b.completed_at DESC LIMIT 1""",
            (FROZEN_BASELINE_ID, FROZEN_PROFILE_ID),
        )
        default_reference = cur.fetchone()
        cur.execute(
            """SELECT b.block_id,b.status,c.status AS campaign_status
               FROM charm_control.experiment_v2_screening_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               ORDER BY b.created_at DESC LIMIT 1"""
        )
        existing = cur.fetchone()
    if baseline is None or baseline["approved"] is not True:
        raise ValueError("screening requires the approved scale-500 baseline")
    if uuid.UUID(str(baseline["preflight_id"])) != preflight_id:
        raise ValueError("frozen preflight does not own the screening baseline")
    if baseline["restore_mechanism"] != FROZEN_RESTORE_MECHANISM:
        raise ValueError("screening requires the selected logical restore")
    if default_reference is None or default_reference["campaign_status"] != "STOPPED":
        raise ValueError("screening requires the terminal passed default-reference block")
    target = _target_safety(settings)
    metadata = discover_knobs(settings, set(PARAMETER_ORDER))
    for point in screening_sobol_configurations(payload):
        configuration = point["configuration"]
        if not isinstance(configuration, dict):
            raise ValueError("generated screening configuration must be an object")
        validate_candidate(
            {str(name): str(value) for name, value in configuration.items()}, metadata
        )
    defaults = {str(name): str(value) for name, value in payload["default_configuration"].items()}
    validate_candidate(defaults, metadata)
    boot = {str(row["name"]): str(row["boot_val"]) for row in metadata}
    if defaults != boot:
        raise ValueError("screening default vector differs from PostgreSQL boot defaults")
    return {
        "ready": (
            payload.get("status") == "ready"
            and payload.get("execution_ready") is True
            and payload["prerequisites"].get("durable_runner_implemented") is True
            and payload["runtime"].get("accepted") is True
            and existing is None
            and target["active_campaigns"] == 0
        ),
        "manifest_status": payload.get("status"),
        "execution_ready": payload.get("execution_ready"),
        "manifest_sha256": manifest_sha256,
        "preflight_id": str(preflight_id),
        "baseline_id": str(FROZEN_BASELINE_ID),
        "benchmark_profile_id": FROZEN_PROFILE_ID,
        "default_reference": {
            "block_id": str(default_reference["block_id"]),
            "status": str(default_reference["status"]),
            "campaign_status": str(default_reference["campaign_status"]),
        },
        "initial_observations": 35,
        "maximum_observations": 41,
        "existing_block": (
            {
                "block_id": str(existing["block_id"]),
                "status": str(existing["status"]),
                "campaign_status": str(existing["campaign_status"]),
            }
            if existing is not None
            else None
        ),
        "target_safety": target,
    }


def create_screening_plan(
    settings: Settings,
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest_path: Path = SCREENING_MANIFEST,
) -> uuid.UUID:
    readiness = screening_readiness(settings, preflight_id, manifest_path)
    if readiness["ready"] is not True:
        raise ValueError("screening execution is blocked until every readiness gate passes")
    _, payload = screening_manifest_payload(manifest_path)
    design = dict(payload["design"])
    campaign_id = create_campaign(
        settings,
        "v2-parameter-screening",
        "CALIBRATION",
        {"maximize": "throughput_tps", "minimize": "p99_ms"},
        {"failures": 0, "p99_slo_applied": False},
        failure_limit=41,
        campaign_settings={
            "protocol_id": "thesis-protocol-v2",
            "stage": SCREENING_STAGE,
            "manifest_sha256": readiness["manifest_sha256"],
            "preflight_id": str(preflight_id),
            "candidate_restore_baseline_id": str(FROZEN_BASELINE_ID),
            "candidate_restore_gate": "required",
            "restore_mechanism": FROZEN_RESTORE_MECHANISM,
            "benchmark_profile_id": FROZEN_PROFILE_ID,
            "pgbench_maintenance_policy": FROZEN_MAINTENANCE_POLICY,
            "sobol_design_sha256": design["sobol_design_sha256"],
            "initial_schedule_sha256": design["initial_schedule_sha256"],
            "manifest": payload,
        },
        actor="v2-screening",
    )
    block_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{campaign_id}:v2-screening")
    rows = screening_plan_rows(payload, block_id, campaign_id)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_screening_blocks
               (block_id,campaign_id,protocol_id,evidence_role,manifest_sha256,
                preflight_id,baseline_id,benchmark_profile_id,status,screening_seed,
                sobol_design_sha256,initial_schedule_sha256,analysis_plan)
               VALUES (%s,%s,'thesis-protocol-v2','CALIBRATION',%s,%s,%s,%s,
                       'INITIAL_PLANNED',%s,%s,%s,%s)""",
            (
                block_id,
                campaign_id,
                readiness["manifest_sha256"],
                preflight_id,
                FROZEN_BASELINE_ID,
                FROZEN_PROFILE_ID,
                design["screening_seed"],
                design["sobol_design_sha256"],
                design["initial_schedule_sha256"],
                Jsonb(payload["analysis_plan"]),
            ),
        )
        for row in rows:
            cur.execute(
                """INSERT INTO charm_control.experiment_v2_screening_runs
                   (run_id,block_id,campaign_id,sequence,chronological_execution_index,
                    phase,evaluation_kind,sobol_index,random_seed,
                    requested_configuration,status)
                   VALUES (%s,%s,%s,%s,%s,'INITIAL',%s,%s,%s,%s,'PLANNED')""",
                (
                    row["run_id"],
                    block_id,
                    campaign_id,
                    row["sequence"],
                    row["chronological_execution_index"],
                    row["evaluation_kind"],
                    row["sobol_index"],
                    row["random_seed"],
                    Jsonb(row["requested_configuration"]),
                ),
            )
        conn.commit()
    return campaign_id


def _screening_block(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT b.*,c.status AS campaign_status
               FROM charm_control.experiment_v2_screening_blocks b
               JOIN charm_control.campaigns c USING(campaign_id)
               WHERE b.campaign_id=%s""",
            (campaign_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown v2 screening campaign {campaign_id}")
    return dict(row)


def _reconcile_screening_run(
    settings: Settings,
    block_id: uuid.UUID,
) -> dict[str, Any] | None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.run_id,r.trial_id,r.status,t.state,t.completed_at,t.failure_type
               FROM charm_control.experiment_v2_screening_runs r
               JOIN charm_control.trials t USING(trial_id)
               WHERE r.block_id=%s AND r.status='CREATED'
               ORDER BY r.sequence LIMIT 1""",
            (block_id,),
        )
        row = cur.fetchone()
        if row is None or row["completed_at"] is None:
            return dict(row) if row is not None else None
        passed = row["state"] == "COMPLETED"
        status = "COMPLETED" if passed else "FAILED"
        failure_details = (
            {} if passed else {"trial_state": row["state"], "failure_type": row["failure_type"]}
        )
        cur.execute(
            """UPDATE charm_control.experiment_v2_screening_runs
               SET status=%s,failure_details=%s,completed_at=clock_timestamp()
               WHERE run_id=%s AND status='CREATED'""",
            (status, Jsonb(failure_details), row["run_id"]),
        )
        conn.commit()
    return {**dict(row), "run_status": status}


def _fail_screening_infrastructure(
    settings: Settings,
    campaign_id: uuid.UUID,
    block_id: uuid.UUID,
    reconciled: dict[str, Any],
    owner: str,
) -> ScreeningStep:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_screening_blocks
               SET status='FAILED',completed_at=clock_timestamp()
               WHERE block_id=%s AND status NOT IN ('PASSED','BLOCKED','FAILED')""",
            (block_id,),
        )
        conn.commit()
    block = _screening_block(settings, campaign_id)
    if block["campaign_status"] in {"RUNNING", "PAUSED"}:
        control_campaign(
            settings,
            campaign_id,
            "stop",
            f"screening infrastructure trial {reconciled['trial_id']} failed",
            actor=owner,
        )
    return ScreeningStep(
        "infrastructure-failed",
        campaign_id,
        block_id,
        uuid.UUID(str(reconciled["run_id"])),
        uuid.UUID(str(reconciled["trial_id"])),
        {"failure_type": reconciled.get("failure_type")},
    )


def run_screening_next(
    settings: Settings,
    campaign_id: uuid.UUID,
    *,
    owner: str = "v2-screening",
    lease_seconds: int = 600,
) -> ScreeningStep:
    block = _screening_block(settings, campaign_id)
    block_id = uuid.UUID(str(block["block_id"]))
    reconciled = _reconcile_screening_run(settings, block_id)
    infrastructure_failures = {"DATASET_RESTORE_FAILED", "BASELINE_FINGERPRINT_FAILED"}
    if reconciled is not None and reconciled.get("run_status") == "FAILED":
        if reconciled.get("failure_type") in infrastructure_failures:
            return _fail_screening_infrastructure(
                settings, campaign_id, block_id, reconciled, owner
            )
        return ScreeningStep(
            "candidate-failure-retained",
            campaign_id,
            block_id,
            uuid.UUID(str(reconciled["run_id"])),
            uuid.UUID(str(reconciled["trial_id"])),
            {"failure_type": reconciled.get("failure_type")},
        )
    if block["campaign_status"] != "RUNNING":
        raise ValueError(f"screening campaign must be RUNNING, not {block['campaign_status']}")
    transitions = {"INITIAL_PLANNED": "INITIAL_RUNNING", "OAT_PLANNED": "OAT_RUNNING"}
    if block["status"] in transitions:
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_screening_blocks
                   SET status=%s WHERE block_id=%s AND status=%s""",
                (transitions[block["status"]], block_id, block["status"]),
            )
            conn.commit()
        block["status"] = transitions[block["status"]]
    if block["status"] not in {"INITIAL_RUNNING", "OAT_RUNNING"}:
        raise ValueError(f"screening block cannot run from {block['status']}")
    if reconciled is not None and reconciled.get("completed_at") is None:
        result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
        finalized = _reconcile_screening_run(settings, block_id)
        if finalized is not None and finalized.get("run_status") == "FAILED":
            if finalized.get("failure_type") in infrastructure_failures:
                return _fail_screening_infrastructure(
                    settings, campaign_id, block_id, finalized, owner
                )
            return ScreeningStep(
                "candidate-failure-retained",
                campaign_id,
                block_id,
                uuid.UUID(str(finalized["run_id"])),
                uuid.UUID(str(finalized["trial_id"])),
                {"failure_type": finalized.get("failure_type")},
            )
        return ScreeningStep(
            "run-executed",
            campaign_id,
            block_id,
            uuid.UUID(str(reconciled["run_id"])),
            result.trial_id,
            {"trial_state": result.state},
        )
    phase = "INITIAL" if block["status"] == "INITIAL_RUNNING" else "OAT"
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_v2_screening_runs
               WHERE block_id=%s AND phase=%s AND status='PLANNED'
               ORDER BY sequence LIMIT 1""",
            (block_id, phase),
        )
        planned_row = cur.fetchone()
    if planned_row is None:
        next_status = "INITIAL_COMPLETE" if phase == "INITIAL" else "OAT_COMPLETE"
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_screening_blocks
                   SET status=%s WHERE block_id=%s AND status=%s""",
                (next_status, block_id, block["status"]),
            )
            conn.commit()
        control_campaign(
            settings,
            campaign_id,
            "pause",
            f"screening {phase.lower()} observations complete; analysis required",
            actor=owner,
        )
        return ScreeningStep(
            "phase-complete", campaign_id, block_id, None, None, {"phase": phase}
        )
    planned = dict(planned_row)
    run_id = uuid.UUID(str(planned["run_id"]))
    evaluation_role = (
        "SCREENING_DEFAULT_CONTROL"
        if planned["evaluation_kind"] == "DEFAULT_CONTROL"
        else "SCREENING_CANDIDATE"
    )
    trial_id = create_v2_tuned_benchmark_trial(
        settings,
        campaign_id,
        uuid.UUID(str(block["preflight_id"])),
        {str(name): str(value) for name, value in planned["requested_configuration"].items()},
        int(planned["random_seed"]),
        f"v2-screening:{run_id}",
        evidence_role="CALIBRATION",
        evaluation_role=evaluation_role,
        warmup_seconds=FROZEN_WARMUP_SECONDS,
        duration_seconds=FROZEN_MEASUREMENT_SECONDS,
        concurrency=FROZEN_CONCURRENCY,
        client_threads=FROZEN_CLIENT_THREADS,
        restore_mechanism=FROZEN_RESTORE_MECHANISM,
        benchmark_profile=FROZEN_PROFILE_ID,
        runtime_samples_required=True,
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_screening_runs
               SET status='CREATED',trial_id=%s WHERE run_id=%s AND status='PLANNED'""",
            (trial_id, run_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("screening run lost its PLANNED state")
        conn.commit()
    result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
    finalized = _reconcile_screening_run(settings, block_id)
    if finalized is not None and finalized.get("run_status") == "FAILED":
        if finalized.get("failure_type") in infrastructure_failures:
            return _fail_screening_infrastructure(settings, campaign_id, block_id, finalized, owner)
        return ScreeningStep(
            "candidate-failure-retained",
            campaign_id,
            block_id,
            run_id,
            trial_id,
            {"failure_type": finalized.get("failure_type")},
        )
    return ScreeningStep(
        "run-executed",
        campaign_id,
        block_id,
        run_id,
        trial_id,
        {
            "trial_state": result.state,
            "phase": phase,
            "chronological_execution_index": planned["chronological_execution_index"],
        },
    )


def screening_history(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    block = _screening_block(settings, campaign_id)
    block_id = uuid.UUID(str(block["block_id"]))
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.*,t.state,t.objective_values,t.constraint_values,t.workflow_result,
                      t.started_at AS trial_started_at,t.completed_at AS trial_completed_at,
                      cr.restore_id,cr.duration_seconds AS restore_seconds,
                      cr.exact_core_passed,cr.physical_statistics_passed
               FROM charm_control.experiment_v2_screening_runs r
               LEFT JOIN charm_control.trials t USING(trial_id)
               LEFT JOIN charm_control.experiment_candidate_dataset_restores cr
                 ON cr.restore_id=t.candidate_dataset_restore_id
               WHERE r.block_id=%s ORDER BY r.sequence""",
            (block_id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        for key, value in tuple(row.items()):
            if isinstance(value, uuid.UUID):
                row[key] = str(value)
        if row.get("trial_started_at") is not None and row.get("trial_completed_at") is not None:
            row["total_seconds"] = (
                row["trial_completed_at"] - row["trial_started_at"]
            ).total_seconds()
    block_payload = {
        key: str(value) if isinstance(value, uuid.UUID) else value for key, value in block.items()
    }
    return {"block": block_payload, "runs": rows}


def _analysis_points(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for row in rows:
        objectives = dict(row.get("objective_values") or {})
        constraints = dict(row.get("constraint_values") or {})
        points.append(
            {
                "run_id": row["run_id"],
                "trial_id": row.get("trial_id"),
                "phase": row["phase"],
                "evaluation_kind": row["evaluation_kind"],
                "sobol_index": row.get("sobol_index"),
                "oat_parameter": row.get("oat_parameter"),
                "chronological_execution_index": row["chronological_execution_index"],
                "requested_configuration": dict(row["requested_configuration"]),
                "status": row["status"],
                "throughput_tps": objectives.get("throughput_tps"),
                "p99_ms": objectives.get("p99_ms"),
                "failures": constraints.get("failures", 0),
                "failure_details": row.get("failure_details") or {},
                "restore_id": row.get("restore_id"),
                "restore_seconds": row.get("restore_seconds"),
                "exact_core_passed": row.get("exact_core_passed"),
                "physical_statistics_passed": row.get("physical_statistics_passed"),
                "total_seconds": row.get("total_seconds"),
            }
        )
    return points


def _oat_configuration(payload: dict[str, Any], name: str, endpoint: str) -> dict[str, str]:
    if endpoint not in {"lower", "upper"}:
        raise ValueError("OAT endpoint must be lower or upper")
    configuration = {
        str(key): str(value) for key, value in payload["default_configuration"].items()
    }
    specification = dict(payload["search_space"]["parameters"][name])
    configuration[name] = _decimal_text(Decimal(str(specification[endpoint])))
    return configuration


def analyze_screening(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    history = screening_history(settings, campaign_id)
    block = history["block"]
    if block["campaign_status"] != "PAUSED":
        raise ValueError("screening analysis requires a clean paused campaign")
    if block["status"] not in {"INITIAL_COMPLETE", "OAT_COMPLETE"}:
        raise ValueError("screening analysis requires a completed phase")
    manifest_sha256, payload = screening_manifest_payload()
    if manifest_sha256 != block["manifest_sha256"]:
        raise ValueError("screening manifest differs from the campaign's frozen manifest")
    points = _analysis_points(history["runs"])
    initial_analysis = (
        dict(block["initial_analysis"]) if block["status"] == "OAT_COMPLETE" else None
    )
    analysis = {
        "campaign_id": str(campaign_id),
        "block_id": str(block["block_id"]),
        "benchmark_profile_id": block["benchmark_profile_id"],
        "sobol_design_sha256": block["sobol_design_sha256"],
        "initial_schedule_sha256": block["initial_schedule_sha256"],
        "analysis_plan": block["analysis_plan"],
        **analyze_screening_observations(
            points,
            payload,
            initial_analysis=initial_analysis,
        ),
    }
    analysis["analysis_sha256"] = _canonical_sha256(analysis)
    outcome = str(analysis["outcome"])
    block_id = uuid.UUID(str(block["block_id"]))
    terminal_status: str | None = None
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        if block["status"] == "INITIAL_COMPLETE" and outcome == "OAT_REQUIRED":
            oat_rows = screening_oat_plan_rows(
                payload,
                block_id,
                campaign_id,
                list(analysis["oat_parameters"]),
            )
            for row in oat_rows:
                cur.execute(
                    """INSERT INTO charm_control.experiment_v2_screening_runs
                       (run_id,block_id,campaign_id,sequence,
                        chronological_execution_index,phase,evaluation_kind,
                        oat_parameter,random_seed,requested_configuration,status)
                       VALUES (%s,%s,%s,%s,%s,'OAT',%s,%s,%s,%s,'PLANNED')""",
                    (
                        row["run_id"],
                        block_id,
                        campaign_id,
                        row["sequence"],
                        row["chronological_execution_index"],
                        row["evaluation_kind"],
                        row["oat_parameter"],
                        row["random_seed"],
                        Jsonb(row["requested_configuration"]),
                    ),
                )
            cur.execute(
                """UPDATE charm_control.experiment_v2_screening_blocks
                   SET status='OAT_PLANNED',initial_analysis=%s,
                       initial_analysis_sha256=%s WHERE block_id=%s""",
                (Jsonb(analysis), analysis["analysis_sha256"], block_id),
            )
        else:
            terminal_status = "PASSED" if outcome == "PASSED" else "BLOCKED"
            if block["status"] == "INITIAL_COMPLETE":
                cur.execute(
                    """UPDATE charm_control.experiment_v2_screening_blocks
                       SET status=%s,initial_analysis=%s,initial_analysis_sha256=%s,
                           final_analysis=%s,final_analysis_sha256=%s,
                           completed_at=clock_timestamp() WHERE block_id=%s""",
                    (
                        terminal_status,
                        Jsonb(analysis),
                        analysis["analysis_sha256"],
                        Jsonb(analysis),
                        analysis["analysis_sha256"],
                        block_id,
                    ),
                )
            else:
                cur.execute(
                    """UPDATE charm_control.experiment_v2_screening_blocks
                       SET status=%s,final_analysis=%s,final_analysis_sha256=%s,
                           completed_at=clock_timestamp() WHERE block_id=%s""",
                    (
                        terminal_status,
                        Jsonb(analysis),
                        analysis["analysis_sha256"],
                        block_id,
                    ),
                )
        conn.commit()
    if terminal_status is not None:
        control_campaign(
            settings,
            campaign_id,
            "stop",
            f"parameter screening resolved as {terminal_status}",
            actor="v2-screening-analysis",
        )
    analysis["block_status"] = terminal_status or "OAT_PLANNED"
    analysis["next_action"] = (
        "explicitly resume the same campaign for the fixed OAT endpoint pairs"
        if terminal_status is None
        else (
            "screening passed; record final knob decisions before primary protocol freeze"
            if terminal_status == "PASSED"
            else "investigate screening validity, drift, or selection failure before primary freeze"
        )
    )
    return analysis


def export_screening_analysis(
    settings: Settings,
    campaign_id: uuid.UUID,
    output: Path | None = None,
) -> dict[str, str]:
    block = _screening_block(settings, campaign_id)
    analysis = block.get("final_analysis")
    analysis_sha256 = block.get("final_analysis_sha256")
    if block["status"] not in {"PASSED", "BLOCKED"} or not isinstance(analysis, dict):
        raise ValueError("screening export requires a finalized analysis")
    if not isinstance(analysis_sha256, str) or len(analysis_sha256) != 64:
        raise ValueError("screening analysis has no valid persisted SHA-256")
    hash_payload = dict(analysis)
    embedded_sha256 = hash_payload.pop("analysis_sha256", None)
    if embedded_sha256 != analysis_sha256 or _canonical_sha256(hash_payload) != analysis_sha256:
        raise ValueError("persisted screening analysis hash does not verify")
    artifact_root = settings.artifact_dir.resolve()
    destination = (
        output.resolve()
        if output is not None
        else (
            artifact_root / "parameter-screening" / f"screening-analysis-{block['block_id']}.json"
        ).resolve()
    )
    try:
        destination.relative_to(artifact_root)
    except ValueError as error:
        raise ValueError("screening analysis export must stay under artifact root") from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(analysis, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return {
        "campaign_id": str(campaign_id),
        "block_id": str(block["block_id"]),
        "analysis_sha256": analysis_sha256,
        "output_path": str(destination),
    }


def screening_step_dict(step: ScreeningStep) -> dict[str, Any]:
    return {
        "action": step.action,
        "campaign_id": str(step.campaign_id),
        "block_id": str(step.block_id),
        "run_id": str(step.run_id) if step.run_id else None,
        "trial_id": str(step.trial_id) if step.trial_id else None,
        "details": step.details,
    }
