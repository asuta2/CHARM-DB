from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

PROTOCOL_ID = "thesis-protocol-v2"
SCHEMA_VERSION = 1

EVIDENCE_ROLES = frozenset(
    {
        "INFRASTRUCTURE",
        "CALIBRATION",
        "HISTORICAL_DIAGNOSTIC",
        "PRIMARY",
        "SECONDARY",
        "F4_CONFIRMATION",
    }
)
MANIFEST_STATUSES = frozenset({"draft", "blocked", "ready"})
RESTORE_GATE_STATES = frozenset({"under-validation", "required", "phase-1-exempt"})
PRIMARY_SEARCH_METHODS = (
    "random",
    "sobol",
    "bo_unconstrained",
    "bo_constrained_qlognei",
    "bo_constrained_qlognehvi",
)
POSTGRESQL_DEFAULT = "postgresql_default"
DURABILITY_KNOBS = frozenset({"fsync", "synchronous_commit", "full_page_writes"})
PRIMARY_FREEZE_GATES = (
    "benchmark_profile_frozen",
    "candidate_restore_policy_frozen",
    "default_reference_passed",
    "parameter_screening_complete",
    "primary_protocol_frozen",
)


@dataclass(frozen=True)
class ProtocolManifest:
    path: Path
    stage: str
    evidence_role: str
    status: str
    artifact_root: PurePosixPath
    unresolved_decisions: tuple[str, ...]
    payload: dict[str, Any]


def _require_mapping(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _validate_artifact_root(value: str) -> PurePosixPath:
    if "\\" in value:
        raise ValueError("artifact_root must use repository-relative POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("artifact_root must stay within the repository")
    if path.parts[:2] != ("artifacts-newpc", "v2"):
        raise ValueError("v2 evidence must use a fresh artifacts-newpc/v2 root")
    return path


def _validate_search_space(payload: dict[str, Any]) -> None:
    search_space = payload.get("search_space")
    if search_space is None:
        return
    parameters = _require_mapping(
        _require_mapping(search_space, "search_space").get("parameters"),
        "search_space.parameters",
    )
    denied = DURABILITY_KNOBS.intersection(parameters)
    if denied:
        raise ValueError(f"durability knobs are forbidden in v2 search spaces: {sorted(denied)}")


def _validate_primary(payload: dict[str, Any], status: str) -> None:
    methods = payload.get("search_methods")
    if methods != list(PRIMARY_SEARCH_METHODS):
        raise ValueError(
            "PRIMARY manifests must list the five v2 search methods in canonical order"
        )
    if POSTGRESQL_DEFAULT in methods:
        raise ValueError("postgresql_default is a blocked reference, not a search method")
    prerequisites = _require_mapping(payload.get("prerequisites"), "prerequisites")
    missing = [gate for gate in PRIMARY_FREEZE_GATES if gate not in prerequisites]
    if missing:
        raise ValueError(f"PRIMARY manifest is missing freeze gates: {missing}")
    if status == "ready":
        failed = [gate for gate in PRIMARY_FREEZE_GATES if prerequisites.get(gate) is not True]
        if failed:
            raise ValueError(f"PRIMARY manifest cannot be ready before gates pass: {failed}")


def validate_manifest(payload: dict[str, Any], *, path: Path | None = None) -> ProtocolManifest:
    payload = _require_mapping(payload, "manifest")
    if payload.get("protocol_id") != PROTOCOL_ID:
        raise ValueError(f"protocol_id must be {PROTOCOL_ID!r}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}")
    stage = _require_string(payload, "stage")
    role = _require_string(payload, "evidence_role")
    if role not in EVIDENCE_ROLES:
        raise ValueError(f"unsupported evidence_role {role!r}")
    status = _require_string(payload, "status")
    if status not in MANIFEST_STATUSES:
        raise ValueError(f"unsupported manifest status {status!r}")
    if not isinstance(payload.get("operator_triggered"), bool):
        raise ValueError("operator_triggered must be Boolean")
    restore_gate = _require_string(payload, "candidate_restore_gate")
    if restore_gate not in RESTORE_GATE_STATES:
        raise ValueError(f"unsupported candidate_restore_gate {restore_gate!r}")
    artifact_root = _validate_artifact_root(_require_string(payload, "artifact_root"))
    prerequisites = payload.get("prerequisites")
    _require_mapping(prerequisites, "prerequisites")
    unresolved = payload.get("unresolved_decisions")
    if not isinstance(unresolved, list) or any(
        not isinstance(item, str) or not item.strip() for item in unresolved
    ):
        raise ValueError("unresolved_decisions must be a list of non-empty strings")
    if status == "ready" and unresolved:
        raise ValueError("ready manifests cannot contain unresolved decisions")
    _validate_search_space(payload)
    if role == "PRIMARY":
        _validate_primary(payload, status)
    return ProtocolManifest(
        path=path or Path("<memory>"),
        stage=stage,
        evidence_role=role,
        status=status,
        artifact_root=artifact_root,
        unresolved_decisions=tuple(unresolved),
        payload=payload,
    )


def load_manifest(path: Path) -> ProtocolManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON in {path}: {error}") from error
    return validate_manifest(_require_mapping(payload, str(path)), path=path)


def validate_manifest_directory(directory: Path) -> list[ProtocolManifest]:
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise ValueError(f"no JSON manifests found in {directory}")
    return [load_manifest(path) for path in paths]


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate CHARM-DB protocol-v2 draft manifests")
    parser.add_argument("directory", nargs="?", type=Path, default=Path("v2/config"))
    args = parser.parse_args()
    manifests = validate_manifest_directory(args.directory)
    for manifest in manifests:
        print(
            f"{manifest.path}: {manifest.status} {manifest.evidence_role} "
            f"({len(manifest.unresolved_decisions)} unresolved)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
