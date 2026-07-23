from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.db import connect
from charmdb.experiment_execution import canonical_manifest_sha256, validate_execution_definition
from charmdb.manifest_preflight import (
    FROZEN_CACHE_POLICY,
    FROZEN_PRACTICAL_MARGINS,
    _target_safety,
)
from charmdb.provenance import source_tree_sha256
from charmdb.resources import capture_and_validate_resources


@dataclass(frozen=True)
class GeneratedManifest:
    preflight_id: uuid.UUID
    reference_block_id: uuid.UUID
    output_path: Path
    execution_sha256: str


@dataclass(frozen=True)
class ManifestDraftResult:
    arm_id: uuid.UUID
    preflight_id: uuid.UUID
    execution_path: Path
    execution_sha256: str
    frozen_manifest_sha256: str


@dataclass(frozen=True)
class ManifestReviewResult:
    review_id: uuid.UUID
    arm_id: uuid.UUID
    preflight_id: uuid.UUID
    passed: bool
    reasons: tuple[str, ...]
    execution_sha256: str
    frozen_manifest_sha256: str


def build_search_execution_definition(
    preflight_id: uuid.UUID,
    evidence: dict[str, Any],
    reference_block_id: uuid.UUID,
    reference_wall_clock_seconds: float,
    reference_trial_ids: list[str] | None = None,
) -> dict[str, Any]:
    dataset = dict(evidence["dataset"])
    profile = dict(evidence["proposed_execution_profile"])
    resources = dict(evidence["resources"])
    container = dict(resources["container"])
    target = dict(evidence["target_safety"])
    software = dict(evidence["software_versions"])
    software["postgresql_target"] = str(target["version"])
    docker_memory = int(
        dict(resources.get("docker_engine", {})).get("memory_total_bytes")
        or container.get("docker_host_memory_bytes")
        or 0
    )
    if docker_memory <= 0:
        raise ValueError("preflight does not contain an exact Docker memory limit")
    execution = {
        "dataset": {
            "snapshot": dataset["snapshot_sha256"],
            "scale": int(dataset["scale"]),
            "schema_sha256": dataset["schema_sha256"],
            "content_sha256": dataset["content_sha256"],
            "snapshot_relative_path": dataset["snapshot_relative_path"],
            "relations": dataset["relations"],
            "relation_rows": dataset["relation_rows"],
        },
        "workload_contexts": [
            {
                "label": "pgbench-transactional-optimization",
                "version": 1,
                "partition": "optimization",
                "workload": "builtin-tpcb-like",
            }
        ],
        "execution_profile": {
            "warmup_seconds": int(profile["warmup_seconds"]),
            "measurement_seconds": int(profile["measurement_seconds"]),
            "concurrency": int(profile["concurrency"]),
            "failure_limit": 5,
        },
        "search_space": {
            "version": "reload-knobs-v1",
            "parameters": {
                "random_page_cost": {"type": "continuous", "lower": 1.0, "upper": 4.0},
                "work_mem": {
                    "type": "categorical",
                    "values": [1024, 2048, 4096, 8192, 16384, 32768],
                },
                "effective_io_concurrency": {
                    "type": "integer",
                    "lower": 0,
                    "upper": 200,
                },
            },
        },
        "method_parameters": {
            "initial_observations": 4,
            "candidate_pool_size": 2048,
            "posterior_samples": 256,
            "max_attempts": 3,
        },
        "resource_limits": {
            "target_cpus": int(container["cpu_limit_nanos"]) / 1_000_000_000,
            "target_memory_bytes": int(container["memory_limit_bytes"]),
            "docker_memory_bytes": docker_memory,
            "target_pids_limit": int(container["pids_limit"]),
        },
        "cache_policy": FROZEN_CACHE_POLICY,
        "software_versions": software,
        "budget_accounting": {
            "kind": "F3_EQUIVALENT_WALL_CLOCK",
            "relative_tolerance": 0.05,
            "f3_reference_wall_clock_seconds": reference_wall_clock_seconds,
            "reference_block_id": str(reference_block_id),
            "reference_trial_ids": reference_trial_ids or [],
        },
        "objective_definition": {
            "maximize": ["throughput_tps", "negative_p99_ms"],
            "reference_point": [0.0, -20.0],
        },
        "constraint_definition": {
            "p99_ms_max": 20.0,
            "failures_max": 0,
            "hard_safety_always_active": True,
        },
        "practical_margins": FROZEN_PRACTICAL_MARGINS,
        "evidence_lineage": {
            "preflight_id": str(preflight_id),
            "reference_block_id": str(reference_block_id),
            "preflight_evidence_sha256": evidence.get("evidence_sha256", ""),
        },
    }
    validate_execution_definition(execution, 20.0)
    return execution


def generate_search_manifest(
    settings: Settings, preflight_id: uuid.UUID, output_path: Path
) -> GeneratedManifest:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT eligible,unresolved,evidence,dataset_content_sha256
               FROM charm_control.experiment_manifest_preflights WHERE preflight_id=%s""",
            (preflight_id,),
        )
        preflight = cur.fetchone()
        if preflight is None:
            raise ValueError(f"unknown manifest preflight {preflight_id}")
        if not bool(preflight["eligible"]) or list(preflight["unresolved"]):
            raise ValueError(f"manifest preflight is not eligible: {preflight['unresolved']}")
        evidence = dict(preflight["evidence"])
        profile = dict(evidence["proposed_execution_profile"])
        cur.execute(
            """
            SELECT b.block_id,b.reference_wall_clock_seconds,b.trial_ids
            FROM charm_control.experiment_f3_reference_blocks b
            JOIN charm_control.experiment_manifest_preflights p USING(preflight_id)
            WHERE b.passed AND b.warmup_seconds=%s AND b.measurement_seconds=%s
              AND b.concurrency=%s AND p.dataset_content_sha256=%s
            ORDER BY b.completed_at DESC LIMIT 1
            """,
            (
                profile["warmup_seconds"],
                profile["measurement_seconds"],
                profile["concurrency"],
                preflight["dataset_content_sha256"],
            ),
        )
        reference = cur.fetchone()
    if reference is None:
        raise ValueError("no compatible passed F3 reference block exists")
    block_id = uuid.UUID(str(reference["block_id"]))
    execution = build_search_execution_definition(
        preflight_id,
        evidence,
        block_id,
        float(reference["reference_wall_clock_seconds"]),
        list(reference["trial_ids"]),
    )
    body = json.dumps(execution, indent=2, sort_keys=True) + "\n"
    if output_path.exists() and output_path.read_text(encoding="utf-8") != body:
        raise ValueError("refusing to overwrite a different generated search manifest")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not output_path.exists():
        temporary = output_path.with_suffix(output_path.suffix + ".tmp")
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(output_path)
    return GeneratedManifest(
        preflight_id,
        block_id,
        output_path,
        canonical_manifest_sha256(execution),
    )


def _sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _load_arm(settings: Settings, arm_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT a.*,g.group_key,g.group_kind,g.hypothesis,g.baseline,g.budget_unit,
                      g.required_metrics,g.prerequisite_gate
               FROM charm_control.experiment_arms a
               JOIN charm_control.experiment_groups g USING(group_id)
               WHERE a.arm_id=%s""",
            (arm_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown experiment arm {arm_id}")
    return dict(row)


def _registry(arm: dict[str, Any]) -> dict[str, Any]:
    return {
        "arm_id": str(arm["arm_id"]),
        "group_id": str(arm["group_id"]),
        "group_key": arm["group_key"],
        "group_kind": arm["group_kind"],
        "hypothesis": arm["hypothesis"],
        "baseline": arm["baseline"],
        "label": arm["label"],
        "method_definition": arm["method_definition"],
        "random_seed": arm["random_seed"],
        "block_order": arm["block_order"],
        "budget_unit": arm["budget_unit"],
        "budget_value": arm["budget_value"],
        "required_metrics": arm["required_metrics"],
        "prerequisite_gate": arm["prerequisite_gate"],
    }


def _assert_next_arm(settings: Settings, arm: dict[str, Any]) -> None:
    if arm["group_key"] != "search_methods":
        raise ValueError("real manifest drafting currently supports only search_methods arms")
    if arm["status"] not in {"PLANNED", "BLOCKED"}:
        raise ValueError(f"arm state {arm['status']} is not draftable")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT arm_id,label,block_order FROM charm_control.experiment_arms
               WHERE group_id=%s AND random_seed=%s AND status NOT IN ('COMPLETED','FAILED')
               ORDER BY block_order LIMIT 1""",
            (arm["group_id"], arm["random_seed"]),
        )
        expected = cur.fetchone()
    if expected is not None and expected["arm_id"] != arm["arm_id"]:
        raise ValueError(
            f"arm is not next in seed block; expected {expected['arm_id']} "
            f"({expected['label']}, order {expected['block_order']})"
        )


def _execution_for_preflight(
    settings: Settings, preflight_id: uuid.UUID
) -> tuple[dict[str, Any], uuid.UUID]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT eligible,unresolved,evidence,evidence_sha256,dataset_content_sha256,
                      schema_sha256
               FROM charm_control.experiment_manifest_preflights WHERE preflight_id=%s""",
            (preflight_id,),
        )
        preflight = cur.fetchone()
        if preflight is None:
            raise ValueError(f"unknown manifest preflight {preflight_id}")
        if not preflight["eligible"] or list(preflight["unresolved"]):
            raise ValueError(f"manifest preflight is not eligible: {preflight['unresolved']}")
        evidence = dict(preflight["evidence"])
        evidence["evidence_sha256"] = str(preflight["evidence_sha256"])
        profile = dict(evidence["proposed_execution_profile"])
        cur.execute(
            """SELECT b.block_id,b.reference_wall_clock_seconds,b.trial_ids
               FROM charm_control.experiment_f3_reference_blocks b
               JOIN charm_control.experiment_manifest_preflights p USING(preflight_id)
               WHERE b.passed AND p.dataset_content_sha256=%s AND p.schema_sha256=%s
                 AND b.warmup_seconds=%s AND b.measurement_seconds=%s AND b.concurrency=%s
               ORDER BY b.completed_at DESC LIMIT 1""",
            (
                preflight["dataset_content_sha256"],
                preflight["schema_sha256"],
                profile["warmup_seconds"],
                profile["measurement_seconds"],
                profile["concurrency"],
            ),
        )
        reference = cur.fetchone()
        cur.execute(
            """SELECT validation_id FROM charm_control.experiment_dataset_restore_validations
               WHERE preflight_id=%s AND passed ORDER BY created_at DESC LIMIT 1""",
            (preflight_id,),
        )
        restore = cur.fetchone()
    if reference is None:
        raise ValueError("no compatible passed F3 reference block exists")
    if restore is None:
        raise ValueError("preflight has no passing dataset restore validation")
    block_id = uuid.UUID(str(reference["block_id"]))
    execution = build_search_execution_definition(
        preflight_id,
        evidence,
        block_id,
        float(reference["reference_wall_clock_seconds"]),
        list(reference["trial_ids"]),
    )
    return execution, block_id


def draft_search_manifest(
    settings: Settings,
    preflight_id: uuid.UUID,
    arm_id: uuid.UUID,
    output: Path | None = None,
) -> ManifestDraftResult:
    arm = _load_arm(settings, arm_id)
    _assert_next_arm(settings, arm)
    execution, _ = _execution_for_preflight(settings, preflight_id)
    body = (json.dumps(execution, indent=2, sort_keys=True, default=str) + "\n").encode()
    output = output or (
        settings.artifact_dir / "experiment-manifests" / f"{arm_id}-{preflight_id}.execution.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and output.read_bytes() != body:
        raise ValueError("refusing to overwrite a different generated search manifest")
    if not output.exists():
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_bytes(body)
        temporary.replace(output)
    frozen = {"schema_version": 1, "registry": _registry(arm), "execution": execution}
    return ManifestDraftResult(
        arm_id,
        preflight_id,
        output,
        _sha256_bytes(body),
        canonical_manifest_sha256(frozen),
    )


def review_search_manifest(
    settings: Settings, preflight_id: uuid.UUID, arm_id: uuid.UUID, path: Path
) -> ManifestReviewResult:
    arm = _load_arm(settings, arm_id)
    reasons: list[str] = []
    try:
        _assert_next_arm(settings, arm)
    except ValueError as exc:
        reasons.append(str(exc))
    raw = path.read_bytes()
    execution_sha256 = _sha256_bytes(raw)
    try:
        candidate = json.loads(raw)
    except json.JSONDecodeError as exc:
        candidate = {}
        reasons.append(f"execution file is not valid JSON: {exc}")
    try:
        expected, _ = _execution_for_preflight(settings, preflight_id)
    except ValueError as exc:
        expected = {}
        reasons.append(str(exc))
    if candidate != expected:
        reasons.append("execution file does not exactly match persisted evidence")
    try:
        validate_execution_definition(candidate, float(arm["budget_value"]))
    except (TypeError, ValueError) as exc:
        reasons.append(f"execution definition is invalid: {exc}")
    safety: dict[str, Any] = {}
    resources: dict[str, Any] = {}
    try:
        safety = _target_safety(settings)
        resources = capture_and_validate_resources(settings)
    except RuntimeError as exc:
        reasons.append(f"live safety/resource review failed: {exc}")
    recorded_source = str(expected.get("software_versions", {}).get("charmdb_source_sha256", ""))
    if recorded_source != source_tree_sha256():
        reasons.append("current source/migration digest differs from preflight evidence")
    frozen = {"schema_version": 1, "registry": _registry(arm), "execution": candidate}
    frozen_sha256 = canonical_manifest_sha256(frozen)
    relative = path.resolve().relative_to(settings.artifact_dir.resolve())
    review_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"charmdb:manifest-review:{preflight_id}:{arm_id}:{execution_sha256}",
    )
    review = {
        "reviewed_at": datetime.now(UTC).isoformat(),
        "target_safety": safety,
        "resources": resources,
        "reasons": reasons,
    }
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_manifest_reviews
               (review_id,preflight_id,arm_id,execution_sha256,frozen_manifest_sha256,
                execution_relative_path,execution_byte_size,passed,reasons,review)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (preflight_id,arm_id,execution_sha256) DO UPDATE
               SET frozen_manifest_sha256=EXCLUDED.frozen_manifest_sha256,
                   execution_relative_path=EXCLUDED.execution_relative_path,
                   execution_byte_size=EXCLUDED.execution_byte_size,
                   passed=EXCLUDED.passed,reasons=EXCLUDED.reasons,review=EXCLUDED.review,
                   created_at=clock_timestamp()""",
            (
                review_id,
                preflight_id,
                arm_id,
                execution_sha256,
                frozen_sha256,
                str(relative),
                len(raw),
                not reasons,
                Jsonb(reasons),
                Jsonb(review),
            ),
        )
        conn.commit()
    return ManifestReviewResult(
        review_id,
        arm_id,
        preflight_id,
        not reasons,
        tuple(reasons),
        execution_sha256,
        frozen_sha256,
    )


def result_json(result: ManifestDraftResult | ManifestReviewResult) -> dict[str, Any]:
    return json.loads(json.dumps(asdict(result), default=str))  # type: ignore[no-any-return]
