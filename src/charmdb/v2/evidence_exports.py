from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, cast

from charmdb.config import Settings
from charmdb.db import connect
from charmdb.v2.screening import PARAMETER_ORDER, partial_rank_correlations
from charmdb.v2.screening_recovery import (
    SOURCE_BLOCK_ID,
    _combined_initial_points,
    screening_recovery_history,
)

EVIDENCE_EXPORT_ROOT = Path("artifacts-newpc/v2/evidence-ledgers")
RECOVERY_CAMPAIGN_ID = uuid.UUID("8565ecfc-a584-46a9-a83f-d4347dbc1eb1")

_TABLE_EXPORTS = {
    "schema-migrations.json": (
        "SELECT * FROM charm_control.schema_migrations ORDER BY version",
        "Complete applied migration ledger",
    ),
    "candidate-restore-ledger.json": (
        """SELECT * FROM charm_control.experiment_candidate_dataset_restores
           ORDER BY created_at,restore_id""",
        "All retained v2 candidate restore attempts and verification details",
    ),
    "saturation-phase1-ladders.json": (
        """SELECT * FROM charm_control.experiment_saturation_phase1_ladders
           ORDER BY scale,anchor_name""",
        "Phase 1 ladder ledger",
    ),
    "saturation-phase1-probes.json": (
        """SELECT * FROM charm_control.experiment_saturation_phase1_probes
           ORDER BY ladder_id,sequence""",
        "Phase 1 probe ledger",
    ),
    "saturation-phase2-configurations.json": (
        """SELECT * FROM charm_control.experiment_saturation_phase2_configurations
           ORDER BY sequence""",
        "Phase 2 full-discipline configuration and trial ledger",
    ),
    "default-reference-ledger.json": (
        """SELECT r.*,b.status AS block_status,
                  b.initial_analysis,b.initial_analysis_sha256,
                  b.final_analysis,b.final_analysis_sha256
           FROM charm_control.experiment_v2_default_reference_runs r
           JOIN charm_control.experiment_v2_default_reference_blocks b USING(block_id)
           ORDER BY r.sequence""",
        "Default-reference run ledger and terminal analysis",
    ),
    "screening-ledger.json": (
        """SELECT r.*,b.status AS block_status,b.initial_analysis,b.final_analysis
           FROM charm_control.experiment_v2_screening_runs r
           JOIN charm_control.experiment_v2_screening_blocks b USING(block_id)
           ORDER BY r.sequence""",
        "Original parameter-screening ledger including failed/planned tail",
    ),
    "screening-recovery-ledger.json": (
        """SELECT r.*,b.status AS block_status,b.initial_analysis,b.final_analysis
           FROM charm_control.experiment_v2_screening_recovery_runs r
           JOIN charm_control.experiment_v2_screening_recovery_blocks b USING(recovery_block_id)
           ORDER BY r.sequence""",
        "Linked screening-recovery ledger",
    ),
    "primary-blocks.json": (
        """SELECT * FROM charm_control.experiment_v2_primary_blocks
           ORDER BY created_at,primary_block_id""",
        "Primary Wave A block, authorization, and terminal analysis ledger",
    ),
    "primary-runs.json": (
        """SELECT * FROM charm_control.experiment_v2_primary_runs
           ORDER BY primary_block_id,global_position""",
        "Immutable primary physical-observation slots and materialized proposals",
    ),
    "primary-attempts.json": (
        """SELECT a.* FROM charm_control.experiment_v2_primary_attempts a
           JOIN charm_control.experiment_v2_primary_runs r USING(primary_run_id)
           ORDER BY r.primary_block_id,r.global_position,a.attempt_number""",
        "Append-only primary slot-attempt and retry ledger",
    ),
    "primary-training-lineage.json": (
        """SELECT l.* FROM charm_control.experiment_v2_primary_training_lineage l
           JOIN charm_control.experiment_v2_primary_runs r
             ON r.primary_run_id=l.primary_run_id
           ORDER BY r.primary_block_id,r.global_position,l.training_position""",
        "Same-seed and same-method primary Bayesian training lineage",
    ),
    "primary-restore-soaks.json": (
        """SELECT * FROM charm_control.experiment_v2_primary_restore_soaks
           ORDER BY created_at,restore_soak_id""",
        "Pre-launch consecutive restore/fingerprint reliability blocks",
    ),
    "primary-restore-soak-runs.json": (
        """SELECT r.* FROM charm_control.experiment_v2_primary_restore_soak_runs r
           JOIN charm_control.experiment_v2_primary_restore_soaks s USING(restore_soak_id)
           ORDER BY s.created_at,r.sequence""",
        "Per-repetition primary restore-soak evidence and failure retention",
    ),
    "multifidelity-phase-b-blocks.json": (
        """SELECT * FROM charm_control.experiment_v2_multifidelity_phase_b_blocks
           ORDER BY created_at,phase_b_block_id""",
        "Live multi-fidelity Phase B block and terminal analysis ledger",
    ),
    "multifidelity-phase-b-runs.json": (
        """SELECT * FROM charm_control.experiment_v2_multifidelity_phase_b_runs
           ORDER BY phase_b_block_id,physical_position""",
        "Immutable Phase B control/candidate slots and promotion decisions",
    ),
    "multifidelity-phase-b-attempts.json": (
        """SELECT a.* FROM charm_control.experiment_v2_multifidelity_phase_b_attempts a
           JOIN charm_control.experiment_v2_multifidelity_phase_b_runs r USING(phase_b_run_id)
           ORDER BY r.phase_b_block_id,r.physical_position,a.attempt_number""",
        "Append-only Phase B attempt and infrastructure-retry ledger",
    ),
    "f4-blocks.json": (
        """SELECT * FROM charm_control.experiment_v2_f4_blocks
           ORDER BY created_at,f4_block_id""",
        "F4 confirmation block, frozen source evidence, and terminal analysis ledger",
    ),
    "f4-runs.json": (
        """SELECT * FROM charm_control.experiment_v2_f4_runs
           ORDER BY f4_block_id,physical_position""",
        "Immutable F4 default/finalist common-seed block schedule",
    ),
    "f4-attempts.json": (
        """SELECT a.* FROM charm_control.experiment_v2_f4_attempts a
           JOIN charm_control.experiment_v2_f4_runs r USING(f4_run_id)
           ORDER BY r.f4_block_id,r.physical_position,a.attempt_number""",
        "Append-only F4 attempt and infrastructure-retry ledger",
    ),
    "infrastructure-failed-trials.json": (
        """SELECT trial_id,campaign_id,state,failure_type,attempt_count,max_attempts,
                  diagnostic_details,workflow_result,created_at,started_at,completed_at
           FROM charm_control.trials
           WHERE protocol_id='thesis-protocol-v2' AND failure_type IS NOT NULL
           ORDER BY created_at,trial_id""",
        "Persisted v2 failed-trial/crash diagnostics",
    ),
    "failed-trial-transitions.json": (
        """SELECT x.* FROM charm_control.trial_transitions x
           JOIN charm_control.trials t USING(trial_id)
           WHERE t.protocol_id='thesis-protocol-v2' AND t.failure_type IS NOT NULL
           ORDER BY x.trial_id,x.occurred_at,x.transition_id""",
        "Transition history for retained v2 failed trials",
    ),
}


def _json_safe(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_payload(output: Path, payload: dict[str, Any]) -> dict[str, Any]:
    core = cast(dict[str, Any], _json_safe(payload))
    wrapped = {**core, "payload_sha256": _canonical_sha256(core)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(wrapped, indent=2) + "\n", encoding="utf-8")
    return {
        "path": str(output.resolve()),
        "payload_sha256": wrapped["payload_sha256"],
        "file_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "rows": len(core.get("rows", [])),
    }


def _screening_prcc_payload(settings: Settings) -> dict[str, Any]:
    history = screening_recovery_history(settings, RECOVERY_CAMPAIGN_ID)
    points, lineage = _combined_initial_points(settings, history)
    sobol = [
        point
        for point in points
        if point["evaluation_kind"] == "SOBOL" and point["status"] == "COMPLETED"
    ]
    configurations = [dict(point["requested_configuration"]) for point in sobol]
    throughput = partial_rank_correlations(
        configurations, [float(point["throughput_tps"]) for point in sobol]
    )
    p99 = partial_rank_correlations(configurations, [float(point["p99_ms"]) for point in sobol])
    leave_one_out_hits = {name: 0 for name in PARAMETER_ORDER}
    for omitted in range(len(sobol)):
        retained = [point for index, point in enumerate(sobol) if index != omitted]
        retained_configurations = [dict(point["requested_configuration"]) for point in retained]
        retained_tps = partial_rank_correlations(
            retained_configurations,
            [float(point["throughput_tps"]) for point in retained],
        )
        retained_p99 = partial_rank_correlations(
            retained_configurations, [float(point["p99_ms"]) for point in retained]
        )
        for name in PARAMETER_ORDER:
            if max(abs(retained_tps[name]), abs(retained_p99[name])) >= 0.20:
                leave_one_out_hits[name] += 1
    scores = [
        {
            "parameter": name,
            "throughput_prcc": throughput[name],
            "p99_prcc": p99[name],
            "maximum_absolute_prcc": max(abs(throughput[name]), abs(p99[name])),
            "leave_one_out_inclusion_frequency": leave_one_out_hits[name] / len(sobol),
        }
        for name in PARAMETER_ORDER
    ]
    return {
        "schema_version": 1,
        "source_block_id": str(SOURCE_BLOCK_ID),
        "recovery_campaign_id": str(RECOVERY_CAMPAIGN_ID),
        "combined_lineage": lineage,
        "valid_sobol_measurements": len(sobol),
        "statistic": "maximum absolute PRCC across throughput TPS and p99 ms",
        "threshold": 0.20,
        "scores": scores,
        "rows": sobol,
    }


def _normalized_utf8_copies(output_root: Path, artifact_root: Path) -> list[dict[str, Any]]:
    source_root = artifact_root / "warmup-and-f3-duration-pilot"
    results: list[dict[str, Any]] = []
    for name in ("duration-history.json", "warmup-analysis.json"):
        source = source_root / name
        if not source.exists():
            continue
        raw = source.read_bytes()
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        payload = json.loads(raw.decode(encoding))
        result = _write_payload(
            output_root / f"{source.stem}-normalized-utf8.json",
            {
                "schema_version": 1,
                "source_path": source.as_posix(),
                "source_file_sha256": hashlib.sha256(raw).hexdigest(),
                "source_encoding": encoding,
                "normalized_payload": payload,
            },
        )
        results.append(result)
    return results


def export_v2_evidence_ledgers(
    settings: Settings,
    output_root: Path | None = None,
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    selected_artifact_root = (artifact_root or settings.artifact_dir).resolve()
    output_root = output_root or selected_artifact_root / "evidence-ledgers"
    allowed_root = selected_artifact_root
    resolved_output = output_root.resolve()
    if resolved_output != allowed_root and allowed_root not in resolved_output.parents:
        raise ValueError(f"v2 evidence exports must stay under {allowed_root}")
    exports: list[dict[str, Any]] = []
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        for filename, (query, description) in _TABLE_EXPORTS.items():
            cur.execute(query)
            rows = [dict(row) for row in cur.fetchall()]
            exports.append(
                _write_payload(
                    output_root / filename,
                    {
                        "schema_version": 1,
                        "description": description,
                        "query": " ".join(query.split()),
                        "rows": rows,
                    },
                )
            )
    exports.append(
        _write_payload(
            output_root / "screening-prcc-recomputation.json",
            _screening_prcc_payload(settings),
        )
    )
    exports.extend(_normalized_utf8_copies(output_root, selected_artifact_root))
    index_payload = {
        "schema_version": 1,
        "analysis_sha256_semantics": "canonical JSON payload hash, not file hash",
        "exports": exports,
    }
    index = _write_payload(output_root / "index.json", index_payload)
    return {"output_root": str(resolved_output), "exports": exports, "index": index}
