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
    "bo_qlognei_throughput",
    "bo_qlognparego_multiobjective",
    "bo_qlognehvi_multiobjective",
)
FINAL_SCREENING_PARAMETERS = (
    "shared_buffers",
    "effective_cache_size",
    "work_mem",
    "checkpoint_timeout",
    "checkpoint_completion_target",
    "max_wal_size",
    "random_page_cost",
    "max_parallel_workers_per_gather",
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
        if payload.get("execution_ready") is not True:
            raise ValueError("PRIMARY manifest cannot be ready before its durable runner passes")
        retry = _require_mapping(
            payload.get("infrastructure_retry_policy"), "infrastructure_retry_policy"
        )
        drift = _require_mapping(payload.get("drift_interpretation"), "drift_interpretation")
        if retry.get("status") != "frozen" or drift.get("status") != "frozen":
            raise ValueError("PRIMARY execution requires supervisor-frozen retry and drift rules")
    profile = _require_mapping(payload.get("benchmark_profile"), "benchmark_profile")
    expected_profile = {
        "id": "scale500-c32-w600-f3-600-v1",
        "scale_factor": 500,
        "warmup_seconds": 600,
        "measurement_seconds": 600,
        "concurrency": 32,
        "client_threads": 4,
        "restore_mechanism": "logical-restore",
        "candidate_restore_before_every_physical_observation": True,
        "unconditional_restart_before_every_measurement": True,
        "pgbench_maintenance_policy": "canonical-baseline-only-no-vacuum",
    }
    if any(profile.get(key) != value for key, value in expected_profile.items()):
        raise ValueError("PRIMARY benchmark profile differs from its frozen executable values")
    search_space = _require_mapping(payload.get("search_space"), "search_space")
    if search_space.get("parameter_order") != list(FINAL_SCREENING_PARAMETERS):
        raise ValueError("PRIMARY search space must use the D035 eight-parameter order")
    parameters = _require_mapping(search_space.get("parameters"), "search_space.parameters")
    if tuple(parameters) != FINAL_SCREENING_PARAMETERS:
        raise ValueError("PRIMARY search-space parameters must exactly match D035")
    if payload.get("candidate_budget_per_method") != 30:
        raise ValueError("PRIMARY Wave A must use 30 candidate slots per method")
    wave_a = _require_mapping(payload.get("wave_a"), "wave_a")
    wave_b = _require_mapping(payload.get("wave_b"), "wave_b")
    if (
        wave_a.get("seed_count") != 3
        or wave_a.get("seeds") != [88408573, 1418705027, 642754166]
        or wave_a.get("candidate_budget_per_method") != 30
        or wave_a.get("physical_observations") != 393
    ):
        raise ValueError("PRIMARY Wave A must use the frozen 30x3 design")
    if (
        wave_b.get("status") != "reserved-not-authorized"
        or wave_b.get("seeds") != [1902413987, 740267717]
        or wave_b.get("physical_observations") != 262
    ):
        raise ValueError("PRIMARY Wave B must preserve the two untouched D025 seeds")
    initial = _require_mapping(payload.get("shared_bo_initial_design"), "shared_bo_initial_design")
    if (
        initial.get("status") != "frozen"
        or initial.get("size") != 12
        or initial.get("candidate_design_sha256")
        != "6664b06c40a484c590b7c66cadbbce9ebad77a5dafbaa86d8bd1ba58719c143e"
    ):
        raise ValueError("PRIMARY must freeze the shared 12-point BO initial design")
    schedule = _require_mapping(payload.get("execution_schedule"), "execution_schedule")
    if (
        schedule.get("schedule_seed") != 1432590420
        or schedule.get("schedule_sha256")
        != "7f1f467e0e1090a9d8e972b06d9ed1efadef8f130f9bc3a2762c7451ddccb7f4"
        or schedule.get("default_controls_per_seed") != 5
    ):
        raise ValueError("PRIMARY must use the frozen Wave A schedule and five controls per seed")
    reference = _require_mapping(
        payload.get("hypervolume_reference_point"), "hypervolume_reference_point"
    )
    if reference.get("status") != "frozen" or reference.get("value") != [0.0, -40.0]:
        raise ValueError("PRIMARY hypervolume reference point must be frozen at (0, -40)")
    constraint_audit = _require_mapping(
        payload.get("learned_constraint_audit"), "learned_constraint_audit"
    )
    if (
        constraint_audit.get("status") != "fallback-reframe-approved"
        or constraint_audit.get("learned_constraint_used") is not False
    ):
        raise ValueError("PRIMARY must use the D036 no-learned-constraint fallback")
    hard_gates = _require_mapping(payload.get("hard_safety_gates"), "hard_safety_gates")
    gates = hard_gates.get("gates")
    required_gate_ids = {
        "manifest-and-preflight-valid",
        "configuration-within-frozen-bounds",
        "target-host-allowlisted",
        "resource-snapshot-within-thresholds",
        "candidate-restore-fingerprint-passed",
        "unconditional-postgresql-restart",
        "active-configuration-verified",
        "complete-benchmark-output",
        "rollback-and-target-health-verified",
    }
    observed_gate_ids = (
        {item.get("id") for item in gates if isinstance(item, dict)}
        if isinstance(gates, list)
        else set()
    )
    validity = _require_mapping(hard_gates.get("objective_validity"), "objective_validity")
    if (
        hard_gates.get("status") != "machine-readable"
        or hard_gates.get("apply_equally_to_all_methods") is not True
        or observed_gate_ids != required_gate_ids
        or validity.get("benchmark_failures_must_equal") != 0
        or validity.get("p99_is_objective_not_constraint") is not True
        or validity.get("no_learned_constraint") is not True
    ):
        raise ValueError("PRIMARY hard-safety gates differ from the D036 contract")
    environment_gates = _require_mapping(
        payload.get("pre_campaign_environment_gates"),
        "pre_campaign_environment_gates",
    )
    if environment_gates != {
        "status": "required",
        "minimum_consecutive_restore_passes": 15,
        "primary_schema_migration": "037_v2_primary_comparison",
        "restore_reliability_schema_migration": "038_v2_primary_restore_reliability",
        "artifact_runtime_directory_must_be_outside_onedrive": True,
        "minimum_artifact_free_bytes": 50 * 1024**3,
        "target_must_have_zero_active_campaigns": True,
    }:
        raise ValueError("PRIMARY pre-campaign environment gates differ from D039")
    retry = _require_mapping(
        payload.get("infrastructure_retry_policy"), "infrastructure_retry_policy"
    )
    if (
        retry.get("maximum_trials_per_slot") != 3
        or retry.get("worker_attempts_per_trial") != 1
        or retry.get("retryable_failure_types")
        != ["DATASET_RESTORE_FAILED", "BASELINE_FINGERPRINT_FAILED"]
        or retry.get("candidate_reused_on_retry") is not True
        or retry.get("new_proposal_on_retry") is not False
        or retry.get("optimizer_training_on_retryable_failure") is not False
        or retry.get("candidate_budget_consumed_by_retry") is not False
        or retry.get("on_exhaustion") != "pause-campaign-preserve-ledger-and-require-human-decision"
    ):
        raise ValueError("PRIMARY infrastructure-retry policy differs from its proposal")
    drift = _require_mapping(payload.get("drift_interpretation"), "drift_interpretation")
    flags = _require_mapping(drift.get("transparency_flags"), "transparency_flags")
    if (
        drift.get("controls") != 15
        or drift.get("primary_adjustment")
        != "within-seed-piecewise-linear-interpolation-of-bracketing-default-controls"
        or drift.get("flag_is_campaign_killing") is not False
        or drift.get("report_raw_and_adjusted_results") is not True
        or drift.get("global_default_constant_for_primary_contrasts") is not False
        or flags.get("maximum_absolute_within_seed_fitted_tps_change_relative") != 0.05
        or flags.get("maximum_absolute_within_seed_fitted_p99_change_ms") != 5.0
    ):
        raise ValueError("PRIMARY drift interpretation differs from its proposal")


def _validate_screening_amendment(payload: dict[str, Any], role: str) -> None:
    if role != "CALIBRATION":
        raise ValueError("screening interpretation amendment must remain CALIBRATION evidence")
    source = _require_mapping(payload.get("source"), "source")
    if (
        source.get("decision") != "D033"
        or source.get("required_outcome") != "BLOCKED_DRIFT"
        or source.get("valid_sobol_measurements") != 32
        or source.get("valid_default_controls") != 3
        or source.get("analysis_sha256")
        != "5a51b941f1511b8f55962a3e5fcf279c71b98e89e94e72db4d5b7b7b2b82b1dd"
    ):
        raise ValueError("screening interpretation amendment must preserve D033 lineage")
    amendment = _require_mapping(payload.get("amendment"), "amendment")
    if (
        amendment.get("decision") != "D035"
        or amendment.get("does_not_rewrite_d033") is not True
        or amendment.get("does_not_claim_d033_passed") is not True
        or amendment.get("oat_waived") is not True
        or amendment.get("additional_screening_observations") != 0
        or amendment.get("screening_phase_treated_as_final") is not True
    ):
        raise ValueError("screening interpretation amendment differs from D035")
    final = _require_mapping(payload.get("final_search_space"), "final_search_space")
    if final.get("parameter_order") != list(FINAL_SCREENING_PARAMETERS):
        raise ValueError("D035 must freeze the exact eight-parameter order")
    parameters = _require_mapping(final.get("parameters"), "final_search_space.parameters")
    if tuple(parameters) != FINAL_SCREENING_PARAMETERS:
        raise ValueError("D035 must freeze exactly eight parameters")


def _validate_screening(payload: dict[str, Any], role: str, status: str) -> None:
    if role != "CALIBRATION":
        raise ValueError("parameter screening must use the CALIBRATION evidence role")
    prerequisites = _require_mapping(payload.get("prerequisites"), "prerequisites")
    for gate in ("benchmark_profile_frozen", "default_reference_passed"):
        if prerequisites.get(gate) is not True:
            raise ValueError(f"parameter screening requires passed prerequisite {gate}")
    design = _require_mapping(payload.get("design"), "design")
    expected = {
        "joint_sobol_configurations": 32,
        "interleaved_default_controls": 3,
        "maximum_targeted_oat_followups": 6,
        "final_dimension_range": [8, 12],
        "required_parameter": "shared_buffers",
    }
    mismatches = {
        key: {"expected": value, "observed": design.get(key)}
        for key, value in expected.items()
        if design.get(key) != value
    }
    if mismatches:
        raise ValueError(f"parameter-screening design differs from v2: {mismatches}")
    seed = design.get("screening_seed")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed <= 0:
        raise ValueError("parameter screening requires a positive integer seed")
    if design.get("default_control_positions") != [1, 18, 35]:
        raise ValueError("parameter screening controls must be fixed at positions 1, 18, and 35")
    analysis = _require_mapping(payload.get("analysis_plan"), "analysis_plan")
    if analysis.get("sensitivity_statistic") != "maximum_absolute_prcc":
        raise ValueError("parameter screening requires the frozen PRCC statistic")
    if analysis.get("inclusion_threshold") != 0.20:
        raise ValueError("parameter screening requires the frozen 0.20 inclusion threshold")
    runtime = _require_mapping(payload.get("runtime"), "runtime")
    if runtime.get("accepted") is not True:
        raise ValueError("parameter-screening runtime must be accepted before design freeze")
    execution_ready = payload.get("execution_ready")
    if not isinstance(execution_ready, bool):
        raise ValueError("parameter screening execution_ready must be Boolean")
    if status == "ready" and (
        execution_ready is not True or prerequisites.get("durable_runner_implemented") is not True
    ):
        raise ValueError("parameter screening cannot be ready before its durable runner passes")


def _validate_screening_recovery(payload: dict[str, Any], role: str, status: str) -> None:
    if role != "CALIBRATION":
        raise ValueError("parameter-screening recovery must use CALIBRATION evidence")
    prerequisites = _require_mapping(payload.get("prerequisites"), "prerequisites")
    required = (
        "benchmark_profile_frozen",
        "default_reference_passed",
        "source_failure_retained",
        "durable_recovery_runner_implemented",
        "restore_stability_validation_required",
    )
    if any(prerequisites.get(gate) is not True for gate in required):
        raise ValueError("parameter-screening recovery prerequisites must be frozen")
    source = _require_mapping(payload.get("source"), "source")
    if (
        source.get("required_status") != "FAILED"
        or source.get("required_campaign_status") != "STOPPED"
    ):
        raise ValueError("screening recovery must retain a stopped failed source block")
    validation = _require_mapping(
        payload.get("restore_stability_validation"), "restore_stability_validation"
    )
    if (
        validation.get("evidence_role") != "INFRASTRUCTURE"
        or validation.get("consecutive_repetitions") != 3
    ):
        raise ValueError("screening recovery requires three infrastructure-only restores")
    schedule = _require_mapping(payload.get("recovery_schedule"), "recovery_schedule")
    observations = schedule.get("observations")
    if not isinstance(observations, list) or [
        item.get("position") if isinstance(item, dict) else None for item in observations
    ] != [34, 35]:
        raise ValueError("screening recovery must contain only frozen positions 34 and 35")
    combined = _require_mapping(payload.get("combined_analysis"), "combined_analysis")
    if (
        combined.get("source_positions") != [1, 33]
        or combined.get("recovery_positions") != [34, 35]
        or combined.get("controls") != [1, 18, 35]
        or combined.get("source_failed_and_planned_rows_remain_immutable") is not True
    ):
        raise ValueError("screening recovery combined-analysis lineage differs from D031")
    runtime = _require_mapping(payload.get("runtime"), "runtime")
    if runtime.get("accepted") is not True:
        raise ValueError("screening-recovery runtime must be accepted")
    if status == "ready" and payload.get("execution_ready") is not True:
        raise ValueError("ready screening recovery requires its durable runner")


def _validate_screening_remediation(payload: dict[str, Any]) -> None:
    supersedes = _require_mapping(payload.get("supersedes"), "supersedes")
    remediation = _require_mapping(payload.get("remediation"), "remediation")
    if (
        supersedes.get("required_status") != "FAILED"
        or not isinstance(supersedes.get("result_sha256"), str)
        or remediation.get("evidence_role") != "INFRASTRUCTURE"
        or remediation.get("method") != "drop-and-recreate-synthetic-target-database"
        or remediation.get("manual_file_deletion_permitted") is not False
        or remediation.get("recovery_source") != "hash-verified frozen logical snapshot"
    ):
        raise ValueError("screening-recovery remediation contract differs from D032")


def _validate_temporal_stability(payload: dict[str, Any], role: str, status: str) -> None:
    if role != "CALIBRATION" or payload.get("evaluation_role") != "DEFAULT_CONTROL":
        raise ValueError("temporal stability must use CALIBRATION default controls")
    source = _require_mapping(payload.get("source"), "source")
    if (
        source.get("decision") != "D033"
        or source.get("required_block_status") != "BLOCKED"
        or source.get("required_campaign_status") != "STOPPED"
        or source.get("required_outcome") != "BLOCKED_DRIFT"
    ):
        raise ValueError("temporal stability must preserve the terminal D033 source")
    purpose = _require_mapping(payload.get("purpose"), "purpose")
    if (
        purpose.get("does_not_salvage_or_reinterpret_d033") is not True
        or purpose.get("does_not_authorize_primary_comparison") is not True
    ):
        raise ValueError("temporal stability cannot reinterpret D033 or authorize primary")
    design = _require_mapping(payload.get("design"), "design")
    if (
        design.get("kind") != "single-fixed-five-default-launch-condition-block"
        or design.get("observations") != 5
        or design.get("no_contingency_or_extension") is not True
        or design.get("maximum_inter_observation_operator_gap_minutes") != 15
    ):
        raise ValueError("temporal stability must remain one fixed five-default block")
    criteria = _require_mapping(payload.get("acceptance_criteria"), "acceptance_criteria")
    if (
        criteria.get("required_valid_observations") != 5
        or criteria.get("maximum_total_observations") != 5
    ):
        raise ValueError("temporal stability cannot add or replace observations")
    runtime = _require_mapping(payload.get("runtime"), "runtime")
    if runtime.get("accepted") is not True:
        raise ValueError("temporal-stability runtime must be accepted before readiness")
    if status == "ready" and payload.get("execution_ready") is not True:
        raise ValueError("ready temporal stability requires its durable runner")
    retirement = _require_mapping(payload.get("retirement"), "retirement")
    if (
        status != "blocked"
        or payload.get("execution_ready") is not False
        or retirement.get("decision") != "D035"
        or retirement.get("retired_without_execution") is not True
        or retirement.get("campaign_created") is not False
        or retirement.get("observations_executed") != 0
        or retirement.get("permanently_prohibit_launch") is not True
    ):
        raise ValueError("D034 must remain retired and non-executable under D035")


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
    if stage == "parameter-screening":
        _validate_screening(payload, role, status)
    if stage in {
        "parameter-screening-recovery",
        "parameter-screening-recovery-remediation",
    }:
        _validate_screening_recovery(payload, role, status)
    if stage == "parameter-screening-recovery-remediation":
        _validate_screening_remediation(payload)
    if stage == "post-screening-temporal-stability":
        _validate_temporal_stability(payload, role, status)
    if stage == "screening-interpretation-amendment":
        _validate_screening_amendment(payload, role)
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
