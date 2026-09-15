from __future__ import annotations

import hashlib
import json
import math
import shutil
import statistics
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from psycopg.types.json import Jsonb

from charmdb.campaigns.default_reference import (
    FROZEN_BASELINE_ID,
    FROZEN_CLIENT_THREADS,
    FROZEN_CONCURRENCY,
    FROZEN_PREFLIGHT_ID,
    FROZEN_RESTORE_MECHANISM,
)
from charmdb.config import Settings
from charmdb.controller import discover_knobs, validate_candidate
from charmdb.db import connect
from charmdb.optimization.design import PRIMARY_MANIFEST, primary_method_design
from charmdb.protocol import load_manifest
from charmdb.restore.fingerprint import capture_live_fingerprint, compare_fingerprints
from charmdb.restore.preflight import _target_safety
from charmdb.worker import (
    control_campaign,
    create_campaign,
    create_tuned_benchmark_trial,
    run_once,
)

MULTIFIDELITY_MANIFEST = Path("experiments/thesis/manifests/multi-fidelity.json")
PHASE_B_STAGE = "multi-fidelity/phase-b"
PHASE_A_ANALYSIS_SHA256 = "12bee9bf819a29c6ac4df377ba0269924fb53d388e9c7807ffd5be6f2edc8ab3"
INFRASTRUCTURE_FAILURES = frozenset({"DATASET_RESTORE_FAILED", "BASELINE_FINGERPRINT_FAILED"})
TERMINAL_RUN_STATUSES = frozenset({"COMPLETED", "CANDIDATE_FAILED", "INFRASTRUCTURE_EXHAUSTED"})
INDEX_FIXTURE_SEQUENCE_MISMATCH_KEYS = frozenset(
    {
        f"sequences.public.charm_index_fixture_id_seq.{field}"
        for field in (
            "start_value",
            "increment_by",
            "min_value",
            "max_value",
            "cache_size",
            "cycle",
            "last_value",
        )
    }
)
INDEX_FIXTURE_COLUMNS = (
    ("id", "bigint", "NO", "YES"),
    ("customer_id", "integer", "NO", "NO"),
    ("status", "text", "NO", "NO"),
    ("created_at", "timestamp with time zone", "NO", "NO"),
    ("amount", "numeric", "NO", "NO"),
    ("payload", "text", "NO", "NO"),
)
INDEX_FIXTURE_CONSTRAINTS = (
    ("charm_index_fixture_amount_not_null", "n"),
    ("charm_index_fixture_created_at_not_null", "n"),
    ("charm_index_fixture_customer_id_not_null", "n"),
    ("charm_index_fixture_id_not_null", "n"),
    ("charm_index_fixture_payload_not_null", "n"),
    ("charm_index_fixture_pkey", "p"),
    ("charm_index_fixture_status_not_null", "n"),
)


@dataclass(frozen=True)
class PhaseBSlot:
    physical_position: int
    slot_kind: str
    control_block: int
    candidate_position: int | None
    random_seed: int
    continuation_seed: int | None
    vector: tuple[float, ...] | None
    configuration: dict[str, str]


@dataclass(frozen=True)
class PhaseBStep:
    action: str
    campaign_id: uuid.UUID
    phase_b_block_id: uuid.UUID
    phase_b_run_id: uuid.UUID | None
    trial_id: uuid.UUID | None
    details: dict[str, Any]


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _derived_seed(label: str) -> int:
    return int.from_bytes(hashlib.sha256(label.encode()).digest()[:4], "big") % 2_147_483_647


def _proposal_sha256(slot: PhaseBSlot) -> str:
    return _canonical_sha256(
        {
            "slot_kind": slot.slot_kind,
            "candidate_position": slot.candidate_position,
            "vector": list(slot.vector) if slot.vector is not None else None,
            "configuration": slot.configuration,
        }
    )


def phase_b_manifest_payload(
    manifest_path: Path = MULTIFIDELITY_MANIFEST,
) -> tuple[str, dict[str, Any]]:
    manifest = load_manifest(manifest_path)
    phase = dict(manifest.payload["phase_b"])
    primary = load_manifest(PRIMARY_MANIFEST).payload
    candidates = primary_method_design(int(phase["seed"]), "sobol", primary)
    rows = [
        {
            "position": position,
            "vector": list(candidate.vector),
            "configuration": candidate.configuration,
        }
        for position, candidate in enumerate(candidates, start=1)
    ]
    if _canonical_sha256(rows) != phase["candidate_design_sha256"]:
        raise ValueError("Phase B candidate design differs from the D054 frozen hash")
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest(), manifest.payload


def build_phase_b_plan(
    manifest_path: Path = MULTIFIDELITY_MANIFEST,
    primary_manifest_path: Path = PRIMARY_MANIFEST,
) -> tuple[PhaseBSlot, ...]:
    _, payload = phase_b_manifest_payload(manifest_path)
    phase = dict(payload["phase_b"])
    primary = load_manifest(primary_manifest_path).payload
    defaults = {
        str(name): str(value)
        for name, value in dict(primary["postgresql_default_configuration"]).items()
    }
    candidates = primary_method_design(int(phase["seed"]), "sobol", primary)
    slots: list[PhaseBSlot] = []
    candidate_position = 0
    for physical_position in range(1, 36):
        control_block = (physical_position - 1) // 7 + 1
        if (physical_position - 1) % 7 == 0:
            slots.append(
                PhaseBSlot(
                    physical_position,
                    "DEFAULT_CONTROL",
                    control_block,
                    None,
                    _derived_seed(
                        f"thesis-protocol-v2/multifidelity/phase-b/control/{control_block}"
                    ),
                    None,
                    None,
                    defaults,
                )
            )
            continue
        candidate_position += 1
        candidate = candidates[candidate_position - 1]
        slots.append(
            PhaseBSlot(
                physical_position,
                "CANDIDATE",
                control_block,
                candidate_position,
                _derived_seed(
                    f"thesis-protocol-v2/multifidelity/phase-b/observation/{physical_position}"
                ),
                _derived_seed(
                    f"thesis-protocol-v2/multifidelity/phase-b/continuation/{candidate_position}"
                ),
                candidate.vector,
                candidate.configuration,
            )
        )
    if len(slots) != 35 or candidate_position != 30:
        raise RuntimeError("Phase B schedule construction did not produce 5+30 slots")
    return tuple(slots)


def _phase_a_gate(settings: Settings) -> dict[str, Any]:
    path = settings.artifact_dir / "multi-fidelity" / "phase-a" / "phase-a-analysis.json"
    if not path.is_file():
        return {"passed": False, "path": str(path), "reason": "analysis-missing"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    observed = _canonical_sha256(payload.get("analysis"))
    passed = (
        observed == payload.get("analysis_sha256") == PHASE_A_ANALYSIS_SHA256
        and payload.get("analysis", {}).get("decision") == "ELIGIBLE_FOR_OPTIONAL_PHASE_B"
    )
    return {
        "passed": passed,
        "path": str(path),
        "analysis_sha256": observed,
        "decision": payload.get("analysis", {}).get("decision"),
    }


def _target_fixture_snapshot(settings: Settings) -> dict[str, Any]:
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT to_regclass('public.charm_index_fixture')::text AS fixture,
                      to_regclass('public.charm_index_fixture_id_seq')::text AS sequence"""
        )
        objects = dict(cur.fetchone() or {})
        cur.execute(
            """SELECT count(*) AS count FROM pg_stat_activity
               WHERE application_name LIKE 'charmdb:%' AND pid<>pg_backend_pid()"""
        )
        active_charm_sessions = int(cur.fetchone()["count"])  # type: ignore[index]
        if objects.get("fixture") is None:
            return {
                **objects,
                "rows": None,
                "columns": [],
                "constraints": [],
                "owned_sequence": None,
                "active_charm_sessions": active_charm_sessions,
                "recognized_test_fixture": False,
            }
        cur.execute("SELECT count(*)::bigint AS count FROM public.charm_index_fixture")
        rows = int(cur.fetchone()["count"])  # type: ignore[index]
        cur.execute(
            """SELECT column_name,data_type,is_nullable,is_identity
               FROM information_schema.columns
               WHERE table_schema='public' AND table_name='charm_index_fixture'
               ORDER BY ordinal_position"""
        )
        columns = [
            (
                str(row["column_name"]),
                str(row["data_type"]),
                str(row["is_nullable"]),
                str(row["is_identity"]),
            )
            for row in cur.fetchall()
        ]
        cur.execute(
            """SELECT conname,contype FROM pg_constraint
               WHERE conrelid='public.charm_index_fixture'::regclass ORDER BY conname"""
        )
        constraints = [(str(row["conname"]), str(row["contype"])) for row in cur.fetchall()]
        cur.execute("SELECT pg_get_serial_sequence('public.charm_index_fixture','id') AS sequence")
        owned_sequence = cur.fetchone()["sequence"]  # type: ignore[index]
    recognized = (
        rows == 50_000
        and tuple(columns) == INDEX_FIXTURE_COLUMNS
        and tuple(constraints) == INDEX_FIXTURE_CONSTRAINTS
        and owned_sequence == "public.charm_index_fixture_id_seq"
        and objects.get("sequence") == "charm_index_fixture_id_seq"
    )
    return {
        **objects,
        "rows": rows,
        "columns": columns,
        "constraints": constraints,
        "owned_sequence": owned_sequence,
        "active_charm_sessions": active_charm_sessions,
        "recognized_test_fixture": recognized,
    }


def _fixture_remediation_blockers(
    campaign: dict[str, Any], attempt: dict[str, Any], target: dict[str, Any]
) -> list[str]:
    verification = dict(attempt.get("verification_details") or {})
    exact_mismatches = set(dict(verification.get("exact_mismatches") or {}))
    physical_mismatches = dict(verification.get("physical_mismatches") or {})
    blockers: list[str] = []
    if campaign.get("campaign_status") != "PAUSED":
        blockers.append("campaign-not-paused")
    if campaign.get("block_status") != "RUNNING":
        blockers.append("phase-b-block-not-running")
    if attempt.get("physical_position") != 1 or attempt.get("attempt_number") != 1:
        blockers.append("failure-is-not-first-slot-attempt-one")
    if attempt.get("run_status") != "CREATED" or attempt.get("attempt_status") != "CREATED":
        blockers.append("durable-attempt-is-not-awaiting-reconciliation")
    if (
        attempt.get("trial_state") != "DATASET_RESTORE_FAILED"
        or attempt.get("failure_type") != "DATASET_RESTORE_FAILED"
    ):
        blockers.append("trial-is-not-a-dataset-restore-failure")
    if attempt.get("exact_core_passed") is not False:
        blockers.append("restore-did-not-fail-the-exact-core-tier")
    if attempt.get("physical_statistics_passed") is not True or physical_mismatches:
        blockers.append("restore-physical-tier-did-not-pass-cleanly")
    if exact_mismatches != INDEX_FIXTURE_SEQUENCE_MISMATCH_KEYS:
        blockers.append("restore-has-mismatches-beyond-the-known-test-sequence")
    if target.get("recognized_test_fixture") is not True:
        blockers.append("target-object-is-not-the-exact-known-test-fixture")
    if target.get("active_charm_sessions") != 0:
        blockers.append("target-has-active-charm-sessions")
    return blockers


def phase_b_fixture_remediation_status(
    settings: Settings, campaign_id: uuid.UUID
) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT c.status AS campaign_status,b.status AS block_status,
                      b.phase_b_block_id
               FROM charm_control.campaigns c
               JOIN charm_control.experiment_v2_multifidelity_phase_b_blocks b
                 USING(campaign_id)
               WHERE c.campaign_id=%s""",
            (campaign_id,),
        )
        campaign_row = cur.fetchone()
        if campaign_row is None:
            raise ValueError(f"unknown Phase B campaign {campaign_id}")
        campaign = dict(campaign_row)
        cur.execute(
            """SELECT r.phase_b_run_id,r.physical_position,r.status AS run_status,
                      a.phase_b_attempt_id,a.attempt_number,a.status AS attempt_status,
                      a.trial_id,t.state AS trial_state,t.failure_type,
                      restore.exact_core_passed,restore.physical_statistics_passed,
                      restore.verification_details
               FROM charm_control.experiment_v2_multifidelity_phase_b_runs r
               JOIN charm_control.experiment_v2_multifidelity_phase_b_attempts a
                 USING(phase_b_run_id)
               JOIN charm_control.trials t USING(trial_id)
               JOIN LATERAL (
                   SELECT exact_core_passed,physical_statistics_passed,verification_details
                   FROM charm_control.experiment_candidate_dataset_restores restore
                   WHERE restore.trial_id=t.trial_id ORDER BY attempt DESC LIMIT 1
               ) restore ON true
               WHERE r.campaign_id=%s AND r.physical_position=1
               ORDER BY a.attempt_number DESC LIMIT 1""",
            (campaign_id,),
        )
        attempt_row = cur.fetchone()
    if attempt_row is None:
        raise ValueError("Phase B campaign has no failed first-slot attempt")
    attempt = dict(attempt_row)
    target = _target_fixture_snapshot(settings)
    blockers = _fixture_remediation_blockers(campaign, attempt, target)
    return {
        "campaign_id": str(campaign_id),
        "campaign": campaign,
        "attempt": attempt,
        "target": target,
        "remediation_allowed": not blockers,
        "blockers": blockers,
    }


def remediate_phase_b_test_fixture(
    settings: Settings, campaign_id: uuid.UUID, *, actor: str = "v2-multifidelity-phase-b"
) -> dict[str, Any]:
    settings.assert_target_allowed()
    before = phase_b_fixture_remediation_status(settings, campaign_id)
    if before["remediation_allowed"] is not True:
        raise ValueError(f"Phase B fixture remediation is blocked: {before['blockers']}")
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("LOCK TABLE public.charm_index_fixture IN ACCESS EXCLUSIVE MODE")
        cur.execute("SELECT count(*)::bigint AS count FROM public.charm_index_fixture")
        if int(cur.fetchone()["count"]) != 50_000:  # type: ignore[index]
            raise RuntimeError("test fixture row count changed while acquiring the cleanup lock")
        cur.execute("SELECT pg_get_serial_sequence('public.charm_index_fixture','id') AS sequence")
        if cur.fetchone()["sequence"] != "public.charm_index_fixture_id_seq":  # type: ignore[index]
            raise RuntimeError("test fixture sequence ownership changed before cleanup")
        cur.execute("DROP TABLE public.charm_index_fixture")
        conn.commit()
    after = _target_fixture_snapshot(settings)
    if after.get("fixture") is not None or after.get("sequence") is not None:
        raise RuntimeError("verified test fixture cleanup did not remove both owned objects")
    block_id = uuid.UUID(str(before["campaign"]["phase_b_block_id"]))
    reconciled = _reconcile_attempt(settings, block_id)
    if reconciled is None or reconciled.get("run_status") != "RETRY_PENDING":
        raise RuntimeError("failed Phase B attempt did not reconcile to RETRY_PENDING")
    audit = {
        "decision": "D056",
        "kind": "verified-test-fixture-remediation",
        "removed_table": "public.charm_index_fixture",
        "removed_owned_sequence": "public.charm_index_fixture_id_seq",
        "verified_rows": 50_000,
        "source_failure_trial_id": str(before["attempt"]["trial_id"]),
        "source_failure_retained": True,
        "reconciled_attempt_status": reconciled.get("attempt_status"),
        "reconciled_run_status": reconciled.get("run_status"),
        "candidate_budget_consumed": False,
        "next_attempt_number": 2,
    }
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.campaigns
               SET settings=jsonb_set(settings,'{phase_b_fixture_remediation}',%s,true),
                   updated_at=clock_timestamp()
               WHERE campaign_id=%s AND status='PAUSED'""",
            (Jsonb(audit), campaign_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Phase B campaign lost its paused remediation state")
        cur.execute(
            """INSERT INTO charm_control.campaign_events
               (campaign_id,event_type,previous_status,new_status,actor,reason,details)
               VALUES (%s,'REMEDIATE','PAUSED','PAUSED',%s,%s,%s)""",
            (
                campaign_id,
                actor,
                "removed verified integration-test fixture after exact fingerprint gate",
                Jsonb(audit),
            ),
        )
        conn.commit()
    return {"before": before, "after": after, "audit": audit}


def validate_phase_b_fixture_remediation(
    settings: Settings, campaign_id: uuid.UUID, *, actor: str = "v2-multifidelity-phase-b"
) -> dict[str, Any]:
    fixture = _target_fixture_snapshot(settings)
    if fixture.get("fixture") is not None or fixture.get("sequence") is not None:
        raise ValueError("test fixture objects remain after Phase B remediation")
    if fixture.get("active_charm_sessions") != 0:
        raise ValueError("target has active CHARM sessions during remediation validation")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT c.status AS campaign_status,b.baseline_id,b.preflight_id,
                      restore.validation_id,baseline.exact_core,
                      baseline.physical_statistics,baseline.physical_tolerances,
                      c.settings->'phase_b_fixture_remediation' AS remediation
               FROM charm_control.campaigns c
               JOIN charm_control.experiment_v2_multifidelity_phase_b_blocks b
                 USING(campaign_id)
               JOIN charm_control.experiment_v2_multifidelity_phase_b_runs r
                 USING(campaign_id)
               JOIN charm_control.experiment_v2_multifidelity_phase_b_attempts a
                 USING(phase_b_run_id)
               JOIN charm_control.experiment_candidate_dataset_restores restore
                 USING(trial_id)
               JOIN charm_control.experiment_candidate_dataset_baselines baseline
                 ON baseline.baseline_id=b.baseline_id
               WHERE c.campaign_id=%s AND r.physical_position=1
                 AND a.attempt_number=1 AND restore.attempt=1""",
            (campaign_id,),
        )
        row = cur.fetchone()
    if row is None or row["campaign_status"] != "PAUSED":
        raise ValueError("Phase B remediation validation requires its paused failed campaign")
    remediation = dict(row["remediation"] or {})
    if remediation.get("decision") != "D056":
        raise ValueError("Phase B remediation audit is absent")
    existing_validation = remediation.get("post_cleanup_validation")
    if isinstance(existing_validation, dict) and existing_validation.get("passed") is True:
        return existing_validation
    exact_core, physical = capture_live_fingerprint(
        settings,
        uuid.UUID(str(row["preflight_id"])),
        uuid.UUID(str(row["validation_id"])),
    )
    comparison = compare_fingerprints(
        dict(row["exact_core"]),
        dict(row["physical_statistics"]),
        exact_core,
        physical,
        dict(row["physical_tolerances"]),
    )
    result = {
        "decision": "D056",
        "passed": comparison.passed,
        "exact_core_passed": comparison.exact_core_passed,
        "physical_statistics_passed": comparison.physical_statistics_passed,
        "exact_mismatches": comparison.exact_mismatches,
        "physical_mismatches": comparison.physical_mismatches,
        "post_cleanup_exact_core_sha256": _canonical_sha256(exact_core),
        "post_cleanup_physical_statistics_sha256": _canonical_sha256(physical),
    }
    if not comparison.passed:
        raise RuntimeError(f"post-remediation baseline fingerprint failed: {result}")
    updated_audit = {**remediation, "post_cleanup_validation": result}
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.campaigns
               SET settings=jsonb_set(settings,'{phase_b_fixture_remediation}',%s,true),
                   updated_at=clock_timestamp()
               WHERE campaign_id=%s AND status='PAUSED'""",
            (Jsonb(updated_audit), campaign_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Phase B campaign lost its paused validation state")
        cur.execute(
            """INSERT INTO charm_control.campaign_events
               (campaign_id,event_type,previous_status,new_status,actor,reason,details)
               VALUES (%s,'REMEDIATION_VALIDATE','PAUSED','PAUSED',%s,%s,%s)""",
            (
                campaign_id,
                actor,
                "verified exact and physical baseline fingerprints after fixture cleanup",
                Jsonb(result),
            ),
        )
        conn.commit()
    return result


def phase_b_readiness(
    settings: Settings, manifest_path: Path = MULTIFIDELITY_MANIFEST
) -> dict[str, Any]:
    manifest_sha256, payload = phase_b_manifest_payload(manifest_path)
    phase = dict(payload["phase_b"])
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('charm_control.experiment_v2_multifidelity_phase_b_blocks') "
            "IS NOT NULL AS installed"
        )
        row = cur.fetchone()
        schema_installed = bool(row and row["installed"])
        existing = None
        if schema_installed:
            cur.execute(
                """SELECT b.phase_b_block_id,b.status,c.status AS campaign_status
                   FROM charm_control.experiment_v2_multifidelity_phase_b_blocks b
                   JOIN charm_control.campaigns c USING(campaign_id)
                   ORDER BY b.created_at DESC LIMIT 1"""
            )
            existing = cur.fetchone()
    metadata = discover_knobs(settings, set(build_phase_b_plan()[0].configuration))
    for slot in build_phase_b_plan(manifest_path):
        validate_candidate(slot.configuration, metadata)
    target = _target_safety(settings, require_idle=False)
    fixture = _target_fixture_snapshot(settings)
    artifact_path = settings.artifact_dir.resolve()
    usage = shutil.disk_usage(artifact_path)
    phase_a = _phase_a_gate(settings)
    blockers: list[str] = []
    if payload["status"] != "ready" or payload.get("execution_ready") is not True:
        blockers.append("manifest-not-execution-ready")
    if phase.get("durable_runner_implemented") is not True:
        blockers.append("durable-runner-not-frozen")
    if not schema_installed:
        blockers.append("migration-039-not-applied")
    if not phase_a["passed"]:
        blockers.append("phase-a-eligibility-not-authenticated")
    if existing is not None:
        blockers.append("phase-b-block-already-exists")
    if target["active_campaigns"] != 0:
        blockers.append("another-campaign-is-active")
    if target["pending_restart"] != 0:
        blockers.append("target-has-pending-restart")
    if target["managed_indexes"] != 0:
        blockers.append("target-has-managed-indexes")
    if target["active_charm_sessions"] != 0:
        blockers.append("target-has-active-charm-sessions")
    if fixture.get("fixture") is not None or fixture.get("sequence") is not None:
        blockers.append("target-has-index-integration-test-fixture")
    if "onedrive" in str(artifact_path).lower():
        blockers.append("artifact-directory-is-inside-onedrive")
    if usage.free < 50 * 1024**3:
        blockers.append("artifact-disk-headroom-below-50-gib")
    return {
        "ready": not blockers,
        "blockers": blockers,
        "manifest_sha256": manifest_sha256,
        "manifest_status": payload["status"],
        "execution_ready": payload.get("execution_ready"),
        "schema_installed": schema_installed,
        "phase_a_gate": phase_a,
        "candidate_design_sha256": phase["candidate_design_sha256"],
        "slots": len(build_phase_b_plan(manifest_path)),
        "existing_block": dict(existing) if existing is not None else None,
        "target_safety": target,
        "test_fixture_gate": fixture,
        "artifact_directory": str(artifact_path),
        "artifact_free_bytes": usage.free,
    }


def create_phase_b_plan(
    settings: Settings, manifest_path: Path = MULTIFIDELITY_MANIFEST
) -> uuid.UUID:
    readiness = phase_b_readiness(settings, manifest_path)
    if readiness["ready"] is not True:
        raise ValueError(f"Phase B is blocked by readiness gates: {readiness['blockers']}")
    _, payload = phase_b_manifest_payload(manifest_path)
    phase = dict(payload["phase_b"])
    campaign_id = create_campaign(
        settings,
        "v2-multifidelity-phase-b",
        "MULTI_FIDELITY_PHASE_B",
        {"maximize": "throughput_tps", "minimize": "p99_ms"},
        {"benchmark_failures_must_equal": 0, "p99_slo_applied": False},
        failure_limit=35,
        campaign_settings={
            "protocol_id": "thesis-protocol-v2",
            "stage": PHASE_B_STAGE,
            "manifest_sha256": readiness["manifest_sha256"],
            "phase_a_analysis_sha256": PHASE_A_ANALYSIS_SHA256,
            "candidate_design_sha256": phase["candidate_design_sha256"],
            "manifest": payload,
        },
        actor="v2-multifidelity-phase-b",
    )
    block_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:{campaign_id}:multifidelity-phase-b")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_multifidelity_phase_b_blocks
               (phase_b_block_id,campaign_id,protocol_id,evidence_role,manifest_sha256,
                phase_a_analysis_sha256,preflight_id,baseline_id,benchmark_profile_id,
                seed,candidate_design_sha256,status)
               VALUES (%s,%s,'thesis-protocol-v2','SECONDARY',%s,%s,%s,%s,%s,%s,%s,'PLANNED')""",
            (
                block_id,
                campaign_id,
                readiness["manifest_sha256"],
                PHASE_A_ANALYSIS_SHA256,
                FROZEN_PREFLIGHT_ID,
                FROZEN_BASELINE_ID,
                phase["benchmark_profile"]["profile_id"],
                phase["seed"],
                phase["candidate_design_sha256"],
            ),
        )
        for slot in build_phase_b_plan(manifest_path):
            run_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"charmdb:{block_id}:run:{slot.physical_position}"
            )
            cur.execute(
                """INSERT INTO charm_control.experiment_v2_multifidelity_phase_b_runs
                   (phase_b_run_id,phase_b_block_id,campaign_id,physical_position,slot_kind,
                    control_block,candidate_position,random_seed,continuation_seed,
                    candidate_vector,requested_configuration,proposal_sha256,status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PROPOSED')""",
                (
                    run_id,
                    block_id,
                    campaign_id,
                    slot.physical_position,
                    slot.slot_kind,
                    slot.control_block,
                    slot.candidate_position,
                    slot.random_seed,
                    slot.continuation_seed,
                    Jsonb(list(slot.vector)) if slot.vector is not None else None,
                    Jsonb(slot.configuration),
                    _proposal_sha256(slot),
                ),
            )
        conn.commit()
    return campaign_id


def _phase_b_block(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT b.*,c.status AS campaign_status
               FROM charm_control.experiment_v2_multifidelity_phase_b_blocks b
               JOIN charm_control.campaigns c USING(campaign_id) WHERE b.campaign_id=%s""",
            (campaign_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown Phase B campaign {campaign_id}")
    return dict(row)


def _reconcile_attempt(settings: Settings, block_id: uuid.UUID) -> dict[str, Any] | None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.phase_b_run_id,r.slot_kind,r.promotion_baseline_tps,
                      a.phase_b_attempt_id,a.attempt_number,a.trial_id,
                      t.state,t.completed_at,t.failure_type,t.objective_values,
                      t.constraint_values,t.workflow_result,t.diagnostic_details
               FROM charm_control.experiment_v2_multifidelity_phase_b_runs r
               JOIN charm_control.experiment_v2_multifidelity_phase_b_attempts a
                 ON a.phase_b_run_id=r.phase_b_run_id AND a.status='CREATED'
               JOIN charm_control.trials t USING(trial_id)
               WHERE r.phase_b_block_id=%s AND r.status='CREATED'
               ORDER BY r.physical_position LIMIT 1""",
            (block_id,),
        )
        row = cur.fetchone()
        if row is None or row["completed_at"] is None:
            return dict(row) if row is not None else None
        result = dict(row)
        failure_type = str(row["failure_type"]) if row["failure_type"] else None
        objectives = dict(row["objective_values"] or {})
        constraints = dict(row["constraint_values"] or {})
        workflow_result = dict(row["workflow_result"] or {})
        promotion = workflow_result.get("promotion")
        failures = int(constraints.get("failures", 0))
        valid = (
            row["state"] == "COMPLETED"
            and failures == 0
            and math.isfinite(float(objectives.get("throughput_tps", math.nan)))
            and math.isfinite(float(objectives.get("p99_ms", math.nan)))
        )
        if row["slot_kind"] == "CANDIDATE":
            valid = (
                valid
                and isinstance(promotion, dict)
                and isinstance(promotion.get("promoted"), bool)
            )
        details = {
            "trial_state": row["state"],
            "failure_type": failure_type,
            "failures": failures,
            "diagnostic_details": dict(row["diagnostic_details"] or {}),
        }
        if valid:
            attempt_status, run_status = "COMPLETED", "COMPLETED"
        elif failure_type in INFRASTRUCTURE_FAILURES:
            attempt_status = "INFRASTRUCTURE_FAILED"
            run_status = (
                "RETRY_PENDING" if int(row["attempt_number"]) < 3 else "INFRASTRUCTURE_EXHAUSTED"
            )
        else:
            attempt_status, run_status = "CANDIDATE_FAILED", "CANDIDATE_FAILED"
        terminal = run_status in TERMINAL_RUN_STATUSES
        cur.execute(
            """UPDATE charm_control.experiment_v2_multifidelity_phase_b_attempts
               SET status=%s,failure_type=%s,failure_details=%s,completed_at=clock_timestamp()
               WHERE phase_b_attempt_id=%s AND status='CREATED'""",
            (attempt_status, failure_type, Jsonb(details), row["phase_b_attempt_id"]),
        )
        cur.execute(
            """UPDATE charm_control.experiment_v2_multifidelity_phase_b_runs
               SET status=%s,infrastructure_attempts=%s,
                   promoted=%s,f2_throughput_tps=%s,f2_failures=%s,reconnect_gap_seconds=%s,
                   failure_details=%s,
                   completed_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END
               WHERE phase_b_run_id=%s AND status='CREATED'""",
            (
                run_status,
                int(row["attempt_number"]),
                promotion.get("promoted") if isinstance(promotion, dict) else None,
                promotion.get("prefix_throughput_tps") if isinstance(promotion, dict) else None,
                promotion.get("prefix_failures") if isinstance(promotion, dict) else None,
                promotion.get("reconnect_gap_seconds") if isinstance(promotion, dict) else None,
                Jsonb(details if run_status != "COMPLETED" else {}),
                terminal,
                row["phase_b_run_id"],
            ),
        )
        result.update({"attempt_status": attempt_status, "run_status": run_status})
        conn.commit()
    return result


def _preceding_control_tps(settings: Settings, block_id: uuid.UUID, position: int) -> float:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT t.objective_values->>'throughput_tps' AS throughput_tps
               FROM charm_control.experiment_v2_multifidelity_phase_b_runs r
               JOIN charm_control.experiment_v2_multifidelity_phase_b_attempts a
                 ON a.phase_b_run_id=r.phase_b_run_id AND a.status='COMPLETED'
               JOIN charm_control.trials t USING(trial_id)
               WHERE r.phase_b_block_id=%s AND r.slot_kind='DEFAULT_CONTROL'
                 AND r.status='COMPLETED' AND r.physical_position<%s
               ORDER BY r.physical_position DESC LIMIT 1""",
            (block_id, position),
        )
        row = cur.fetchone()
    if row is None or row["throughput_tps"] is None:
        raise RuntimeError("candidate has no completed causal preceding control")
    value = float(row["throughput_tps"])
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError("causal preceding control TPS is invalid")
    return value


def _execute_slot(
    settings: Settings,
    campaign_id: uuid.UUID,
    block_id: uuid.UUID,
    row: dict[str, Any],
    owner: str,
    lease_seconds: int,
) -> PhaseBStep:
    run_id = uuid.UUID(str(row["phase_b_run_id"]))
    attempt_number = int(row["infrastructure_attempts"]) + 1
    profile = "scale500-c32-w600-f2-60-promote-to-f3-600-v1"
    promotion_rule = None
    duration = 60
    if row["slot_kind"] == "CANDIDATE":
        baseline_tps = _preceding_control_tps(settings, block_id, int(row["physical_position"]))
        promotion_rule = {
            "prefix_seconds": 60,
            "continuation_seconds": 540,
            "baseline_throughput_tps": baseline_tps,
            "throughput_floor_ratio": 0.8,
            "continuation_seed": int(row["continuation_seed"]),
        }
        duration = 600
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_multifidelity_phase_b_runs
                   SET promotion_baseline_tps=%s
                   WHERE phase_b_run_id=%s AND status IN ('PROPOSED','RETRY_PENDING')
                     AND (promotion_baseline_tps IS NULL OR promotion_baseline_tps=%s)""",
                (baseline_tps, run_id, baseline_tps),
            )
            if cur.rowcount != 1:
                raise RuntimeError("Phase B causal promotion baseline changed across retry")
            conn.commit()
    trial_id = create_tuned_benchmark_trial(
        settings,
        campaign_id,
        FROZEN_PREFLIGHT_ID,
        {str(k): str(v) for k, v in dict(row["requested_configuration"]).items()},
        int(row["random_seed"]),
        f"v2-multifidelity-phase-b:{run_id}:attempt:{attempt_number}",
        evidence_role="SECONDARY",
        evaluation_role=f"MULTIFIDELITY_PHASE_B_{row['slot_kind']}",
        warmup_seconds=600,
        duration_seconds=duration,
        concurrency=FROZEN_CONCURRENCY,
        client_threads=FROZEN_CLIENT_THREADS,
        max_attempts=1,
        restore_mechanism=FROZEN_RESTORE_MECHANISM,
        benchmark_profile=profile,
        runtime_samples_required=True,
        promotion_rule=promotion_rule,
    )
    attempt_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"charmdb:{run_id}:multifidelity-phase-b-attempt:{attempt_number}"
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_v2_multifidelity_phase_b_attempts
               (phase_b_attempt_id,phase_b_run_id,attempt_number,trial_id,status)
               VALUES (%s,%s,%s,%s,'CREATED')
               ON CONFLICT (phase_b_run_id,attempt_number) DO NOTHING""",
            (attempt_id, run_id, attempt_number, trial_id),
        )
        cur.execute(
            """UPDATE charm_control.experiment_v2_multifidelity_phase_b_runs SET status='CREATED'
               WHERE phase_b_run_id=%s AND status IN ('PROPOSED','RETRY_PENDING')""",
            (run_id,),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Phase B slot lost its executable state")
        conn.commit()
    result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
    finalized = _reconcile_attempt(settings, block_id)
    return PhaseBStep(
        "slot-executed",
        campaign_id,
        block_id,
        run_id,
        trial_id,
        {
            "physical_position": row["physical_position"],
            "slot_kind": row["slot_kind"],
            "attempt": attempt_number,
            "trial_state": result.state,
            "run_status": finalized.get("run_status") if finalized else None,
        },
    )


def run_phase_b_next(
    settings: Settings,
    campaign_id: uuid.UUID,
    *,
    owner: str = "v2-multifidelity-phase-b",
    lease_seconds: int = 600,
) -> PhaseBStep:
    block = _phase_b_block(settings, campaign_id)
    block_id = uuid.UUID(str(block["phase_b_block_id"]))
    reconciled = _reconcile_attempt(settings, block_id)
    if reconciled is not None and reconciled.get("run_status") == "INFRASTRUCTURE_EXHAUSTED":
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_multifidelity_phase_b_blocks
                   SET status='PAUSED_INFRASTRUCTURE'
                   WHERE phase_b_block_id=%s AND status='RUNNING'""",
                (block_id,),
            )
            conn.commit()
        control_campaign(settings, campaign_id, "pause", "Phase B retries exhausted", actor=owner)
        return PhaseBStep(
            "infrastructure-paused",
            campaign_id,
            block_id,
            uuid.UUID(str(reconciled["phase_b_run_id"])),
            uuid.UUID(str(reconciled["trial_id"])),
            {"human_decision_required": True, "candidate_budget_consumed": False},
        )
    if block["campaign_status"] != "RUNNING":
        raise ValueError(f"Phase B campaign must be RUNNING, not {block['campaign_status']}")
    if block["status"] == "PLANNED":
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.experiment_v2_multifidelity_phase_b_blocks
                   SET status='RUNNING' WHERE phase_b_block_id=%s AND status='PLANNED'""",
                (block_id,),
            )
            conn.commit()
    elif block["status"] != "RUNNING":
        raise ValueError(f"Phase B block cannot run from {block['status']}")
    if reconciled is not None and reconciled.get("completed_at") is None:
        result = run_once(settings, owner, lease_seconds, campaign_id=campaign_id)
        _reconcile_attempt(settings, block_id)
        return PhaseBStep(
            "slot-resumed", campaign_id, block_id, None, result.trial_id, {"state": result.state}
        )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM charm_control.experiment_v2_multifidelity_phase_b_runs
               WHERE phase_b_block_id=%s AND status IN ('PROPOSED','RETRY_PENDING')
               ORDER BY physical_position LIMIT 1""",
            (block_id,),
        )
        row = cur.fetchone()
    if row is not None:
        return _execute_slot(settings, campaign_id, block_id, dict(row), owner, lease_seconds)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT status,count(*) AS count
               FROM charm_control.experiment_v2_multifidelity_phase_b_runs
               WHERE phase_b_block_id=%s GROUP BY status""",
            (block_id,),
        )
        counts = {str(item["status"]): int(item["count"]) for item in cur.fetchall()}
        if sum(counts.get(status, 0) for status in TERMINAL_RUN_STATUSES) != 35:
            raise RuntimeError(f"Phase B has no runnable slot but is incomplete: {counts}")
        if counts.get("INFRASTRUCTURE_EXHAUSTED", 0):
            raise RuntimeError("Phase B contains an infrastructure-exhausted slot")
        cur.execute(
            """UPDATE charm_control.experiment_v2_multifidelity_phase_b_blocks
               SET status='OBSERVATIONS_COMPLETE'
               WHERE phase_b_block_id=%s AND status='RUNNING'""",
            (block_id,),
        )
        conn.commit()
    control_campaign(
        settings, campaign_id, "pause", "all Phase B observations are terminal", actor=owner
    )
    return PhaseBStep(
        "observations-complete", campaign_id, block_id, None, None, {"counts": counts}
    )


def phase_b_history(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    block = _phase_b_block(settings, campaign_id)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.*,a.attempt_number,a.trial_id,a.status AS attempt_status,
                      t.state AS trial_state,t.objective_values,t.constraint_values,
                      t.workflow_result
               FROM charm_control.experiment_v2_multifidelity_phase_b_runs r
               LEFT JOIN LATERAL (
                   SELECT * FROM charm_control.experiment_v2_multifidelity_phase_b_attempts a
                   WHERE a.phase_b_run_id=r.phase_b_run_id
                   ORDER BY attempt_number DESC LIMIT 1
               ) a ON true LEFT JOIN charm_control.trials t USING(trial_id)
               WHERE r.phase_b_block_id=%s ORDER BY r.physical_position""",
            (block["phase_b_block_id"],),
        )
        runs = [dict(row) for row in cur.fetchall()]
    return {"block": block, "runs": runs}


def _artifact_path(root: Path, relative: object) -> Path:
    value = Path(str(relative))
    if value.is_absolute():
        raise ValueError("durable artifact path must be relative to the configured root")
    resolved_root = root.resolve()
    resolved = (resolved_root / value).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("durable artifact path escapes the configured root")
    return resolved


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_authenticated_phase_b_rows(
    settings: Settings, campaign_id: uuid.UUID
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    block = _phase_b_block(settings, campaign_id)
    if block["status"] != "OBSERVATIONS_COMPLETE" or block["campaign_status"] != "PAUSED":
        raise ValueError("Phase B analysis requires a paused observations-complete block")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.*,a.attempt_number,a.status AS attempt_status,a.trial_id,
                      t.state AS trial_state,t.created_at AS trial_created_at,
                      t.started_at AS trial_started_at,t.completed_at AS trial_completed_at,
                      t.objective_values,t.constraint_values,t.workflow_result,
                      EXTRACT(EPOCH FROM (t.completed_at-t.created_at)) AS lifecycle_seconds,
                      restore.duration_seconds AS restore_seconds,
                      restore.exact_core_passed,restore.physical_statistics_passed,
                      measurement.result AS measurement_result,
                      validation.result AS validation_result,
                      artifact.relative_path AS artifact_relative_path,
                      artifact.sha256 AS artifact_sha256,
                      artifact.byte_size AS artifact_byte_size
               FROM charm_control.experiment_v2_multifidelity_phase_b_runs r
               JOIN charm_control.experiment_v2_multifidelity_phase_b_attempts a
                 ON a.phase_b_run_id=r.phase_b_run_id AND a.status='COMPLETED'
               JOIN charm_control.trials t USING(trial_id)
               JOIN charm_control.experiment_candidate_dataset_restores restore
                 ON restore.restore_id=t.candidate_dataset_restore_id
               JOIN charm_control.trial_action_executions measurement
                 ON measurement.trial_id=t.trial_id
                AND measurement.state='RUNNING_FULL_EVALUATION'
                AND measurement.status='COMPLETED'
               JOIN charm_control.trial_action_executions validation
                 ON validation.trial_id=t.trial_id
                AND validation.state='VALIDATING_MEASUREMENT'
                AND validation.status='COMPLETED'
               JOIN charm_control.artifacts artifact
                 ON artifact.trial_id=t.trial_id AND artifact.kind='durable-trial-json'
               WHERE r.phase_b_block_id=%s ORDER BY r.physical_position""",
            (block["phase_b_block_id"],),
        )
        rows = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT r.physical_position,a.attempt_number,a.status,a.failure_type,
                      t.trial_id,t.state,
                      EXTRACT(EPOCH FROM (t.completed_at-t.created_at)) AS lifecycle_seconds
               FROM charm_control.experiment_v2_multifidelity_phase_b_attempts a
               JOIN charm_control.experiment_v2_multifidelity_phase_b_runs r USING(phase_b_run_id)
               JOIN charm_control.trials t USING(trial_id)
               WHERE r.phase_b_block_id=%s AND a.status<>'COMPLETED'
               ORDER BY r.physical_position,a.attempt_number""",
            (block["phase_b_block_id"],),
        )
        failed_attempts = [dict(row) for row in cur.fetchall()]
    if len(rows) != 35:
        raise ValueError("Phase B analysis requires 35 authenticated completed slots")

    plan = build_phase_b_plan()
    root = settings.artifact_dir.resolve()
    preceding_control_tps: float | None = None
    for row, expected in zip(rows, plan, strict=True):
        identity = (
            int(row["physical_position"]),
            str(row["slot_kind"]),
            int(row["control_block"]),
            int(row["candidate_position"]) if row["candidate_position"] is not None else None,
            int(row["random_seed"]),
            int(row["continuation_seed"]) if row["continuation_seed"] is not None else None,
            tuple(float(value) for value in row["candidate_vector"])
            if row["candidate_vector"] is not None
            else None,
            {str(key): str(value) for key, value in dict(row["requested_configuration"]).items()},
            str(row["proposal_sha256"]),
        )
        expected_identity = (
            expected.physical_position,
            expected.slot_kind,
            expected.control_block,
            expected.candidate_position,
            expected.random_seed,
            expected.continuation_seed,
            expected.vector,
            expected.configuration,
            _proposal_sha256(expected),
        )
        if identity != expected_identity:
            raise ValueError(
                f"Phase B frozen-plan mismatch at position {expected.physical_position}"
            )
        objectives = dict(row["objective_values"] or {})
        constraints = dict(row["constraint_values"] or {})
        workflow = dict(row["workflow_result"] or {})
        measurement = dict(row["measurement_result"] or {})
        validation = dict(row["validation_result"] or {})
        result = dict(measurement.get("result") or {})
        if not all(
            (
                row["status"] == "COMPLETED",
                row["attempt_status"] == "COMPLETED",
                row["trial_state"] == "COMPLETED",
                bool(row["exact_core_passed"]),
                bool(row["physical_statistics_passed"]),
                validation.get("valid") is True,
                int(constraints.get("failures", -1)) == 0,
                int(result.get("failures", -1)) == 0,
            )
        ):
            raise ValueError(f"Phase B hard-gate failure at position {expected.physical_position}")
        for name in ("throughput_tps", "p99_ms"):
            value = float(objectives.get(name, math.nan))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Phase B invalid {name} at position {expected.physical_position}")

        artifact = _artifact_path(root, row["artifact_relative_path"])
        if (
            not artifact.is_file()
            or artifact.stat().st_size != int(row["artifact_byte_size"])
            or _file_sha256(artifact) != str(row["artifact_sha256"])
        ):
            raise ValueError(f"Phase B durable artifact failed authentication: {artifact}")
        artifact_payload = json.loads(artifact.read_text(encoding="utf-8"))
        marker_relative = measurement.get("marker_relative_path")
        if (
            not marker_relative
            or workflow.get("measurement_marker") != marker_relative
            or artifact_payload.get("measurement_marker") != marker_relative
        ):
            raise ValueError("Phase B measurement marker lineage differs between records")
        marker = _artifact_path(root, marker_relative)
        if not marker.is_file() or _file_sha256(marker) != str(measurement.get("marker_sha256")):
            raise ValueError(f"Phase B measurement marker authentication failed: {marker}")
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        marker_result = dict(marker_payload.get("result") or {})
        for name in ("throughput_tps", "p99_ms", "duration_seconds"):
            if not math.isclose(
                float(marker_result.get(name, math.nan)),
                float(workflow.get(name, math.nan)),
                rel_tol=1e-12,
                abs_tol=1e-9,
            ):
                raise ValueError(f"Phase B marker result mismatch for {name}")

        promotion = workflow.get("promotion")
        if expected.slot_kind == "DEFAULT_CONTROL":
            if promotion is not None or row["promoted"] is not None:
                raise ValueError("Phase B control unexpectedly has a promotion decision")
            preceding_control_tps = float(objectives["throughput_tps"])
        else:
            if preceding_control_tps is None or not isinstance(promotion, dict):
                raise ValueError("Phase B candidate lacks a causal promotion record")
            promoted = bool(promotion.get("promoted"))
            f2_tps = float(promotion.get("prefix_throughput_tps", math.nan))
            f2_failures = int(promotion.get("prefix_failures", -1))
            baseline_tps = float(promotion.get("baseline_throughput_tps", math.nan))
            expected_decision = f2_failures == 0 and f2_tps >= 0.8 * baseline_tps
            if not all(
                (
                    promoted == expected_decision,
                    row["promoted"] is promoted,
                    f2_failures == int(row["f2_failures"]),
                    math.isclose(f2_tps, float(row["f2_throughput_tps"]), rel_tol=1e-12),
                    math.isclose(baseline_tps, preceding_control_tps, rel_tol=1e-12),
                    math.isclose(baseline_tps, float(row["promotion_baseline_tps"]), rel_tol=1e-12),
                )
            ):
                raise ValueError(
                    f"Phase B promotion lineage mismatch at position {expected.physical_position}"
                )
            prefix_marker = marker.parent / str(promotion.get("prefix_marker"))
            if not prefix_marker.is_file():
                raise ValueError(f"Phase B prefix marker is missing: {prefix_marker}")
            continuation_relative = promotion.get("continuation_marker")
            if promoted:
                continuation_marker = marker.parent / str(continuation_relative)
                reconnect_gap = float(promotion.get("reconnect_gap_seconds", math.nan))
                if not continuation_marker.is_file() or not math.isfinite(reconnect_gap):
                    raise ValueError("Phase B promoted continuation evidence is incomplete")
            elif continuation_relative is not None:
                raise ValueError(
                    "Phase B rejected candidate unexpectedly has continuation evidence"
                )
        row.update(
            {
                "objective_values": objectives,
                "constraint_values": constraints,
                "workflow_result": workflow,
                "authenticated": True,
                "marker_relative_path": str(marker_relative),
                "marker_sha256": str(measurement["marker_sha256"]),
            }
        )
    return rows, failed_attempts


def analyze_phase_b_observations(
    rows: list[dict[str, Any]],
    failed_attempts: list[dict[str, Any]],
    payload: dict[str, Any],
) -> dict[str, Any]:
    if len(rows) != 35:
        raise ValueError("Phase B analysis requires the complete 35-slot ledger")
    controls = [row for row in rows if row["slot_kind"] == "DEFAULT_CONTROL"]
    candidates = [row for row in rows if row["slot_kind"] == "CANDIDATE"]
    if len(controls) != 5 or len(candidates) != 30:
        raise ValueError("Phase B analysis requires five controls and 30 candidates")
    if not all(row.get("authenticated") is True for row in rows):
        raise ValueError("Phase B analysis refuses unauthenticated observations")
    promoted = [row for row in candidates if row["promoted"] is True]
    rejected = [row for row in candidates if row["promoted"] is False]
    successful_lifecycle_seconds = sum(float(row["lifecycle_seconds"]) for row in rows)
    failed_lifecycle_seconds = sum(float(row["lifecycle_seconds"]) for row in failed_attempts)
    observed_total_seconds = successful_lifecycle_seconds + failed_lifecycle_seconds
    saved_seconds = 540.0 * len(rejected)
    counterfactual_total_seconds = observed_total_seconds + saved_seconds
    planned_maximum_seconds = (
        float(
            payload["phase_b"]["runtime"]["restore-inclusive_hours_before_retries_or-operator-gaps"]
        )
        * 3600.0
    )
    predicted_successful_seconds = planned_maximum_seconds - saved_seconds
    time_model_error_relative = (
        abs(successful_lifecycle_seconds - predicted_successful_seconds)
        / predicted_successful_seconds
    )
    stage_timing_errors: list[float] = []
    for row in rows:
        expected = 600.0 if row["slot_kind"] == "CANDIDATE" and row["promoted"] else 60.0
        observed = float(row["workflow_result"]["duration_seconds"])
        stage_timing_errors.append(abs(observed - expected) / expected)
    control_tps = [float(row["objective_values"]["throughput_tps"]) for row in controls]
    control_p99 = [float(row["objective_values"]["p99_ms"]) for row in controls]
    reconnect_gaps = [float(row["reconnect_gap_seconds"]) for row in promoted]
    f3_tps = [float(row["objective_values"]["throughput_tps"]) for row in promoted]
    f3_p99 = [float(row["objective_values"]["p99_ms"]) for row in promoted]
    maximum_error = float(
        payload["phase_b"]["success_rule"]["maximum_absolute-time-model-error-relative"]
    )
    gates = {
        "measured_total_time_lower_than_counterfactual_f3_only": (
            observed_total_seconds < counterfactual_total_seconds
        ),
        "absolute_time_model_error_relative": time_model_error_relative,
        "maximum_absolute_time_model_error_relative": maximum_error,
        "time_model_error_passed": time_model_error_relative <= maximum_error,
        "all_failures_and_retries_retained": (
            sum(int(row["infrastructure_attempts"]) for row in rows)
            == len(rows) + len(failed_attempts)
        ),
    }
    passed = all(
        (
            gates["measured_total_time_lower_than_counterfactual_f3_only"],
            gates["time_model_error_passed"],
            gates["all_failures_and_retries_retained"],
        )
    )
    return {
        "decision": "LIVE_DEMONSTRATION_PASSED" if passed else "LIVE_DEMONSTRATION_FAILED",
        "evidence_role": "SECONDARY",
        "passed": passed,
        "success_gates": gates,
        "counts": {
            "physical_slots": len(rows),
            "controls": len(controls),
            "candidates": len(candidates),
            "promoted": len(promoted),
            "rejected": len(rejected),
            "promotion_rate": len(promoted) / len(candidates),
            "valid_zero_failure_measurements": sum(
                int(row["constraint_values"]["failures"]) == 0 for row in rows
            ),
            "passed_restore_fingerprints": sum(
                bool(row["exact_core_passed"] and row["physical_statistics_passed"]) for row in rows
            ),
            "retained_failed_infrastructure_attempts": len(failed_attempts),
            "total_attempts": sum(int(row["infrastructure_attempts"]) for row in rows),
        },
        "time": {
            "planned_maximum_pre_retry_seconds": planned_maximum_seconds,
            "predicted_successful_seconds_after_rejections": predicted_successful_seconds,
            "observed_successful_lifecycle_seconds": successful_lifecycle_seconds,
            "retained_failed_infrastructure_seconds": failed_lifecycle_seconds,
            "observed_total_including_failed_seconds": observed_total_seconds,
            "counterfactual_f3_only_seconds": counterfactual_total_seconds,
            "saved_seconds": saved_seconds,
            "saved_hours": saved_seconds / 3600.0,
            "saved_percent_of_counterfactual_total": (
                saved_seconds / counterfactual_total_seconds * 100.0
            ),
            "maximum_stage_duration_error_relative": max(stage_timing_errors),
            "mean_stage_duration_error_relative": statistics.fmean(stage_timing_errors),
        },
        "controls_descriptive": {
            "positions": [int(row["physical_position"]) for row in controls],
            "throughput_tps": control_tps,
            "p99_ms": control_p99,
            "first_to_last_throughput_change_relative": control_tps[-1] / control_tps[0] - 1.0,
            "first_to_last_p99_change_ms": control_p99[-1] - control_p99[0],
            "throughput_cv_relative": statistics.stdev(control_tps) / statistics.fmean(control_tps),
        },
        "promoted_f3_descriptive": {
            "mean_throughput_tps": statistics.fmean(f3_tps),
            "minimum_throughput_tps": min(f3_tps),
            "maximum_throughput_tps": max(f3_tps),
            "mean_p99_ms": statistics.fmean(f3_p99),
            "minimum_p99_ms": min(f3_p99),
            "maximum_p99_ms": max(f3_p99),
        },
        "rejected_candidates": [
            {
                "candidate_position": int(row["candidate_position"]),
                "physical_position": int(row["physical_position"]),
                "f2_throughput_tps": float(row["f2_throughput_tps"]),
                "causal_control_tps": float(row["promotion_baseline_tps"]),
                "control_relative_ratio": (
                    float(row["f2_throughput_tps"]) / float(row["promotion_baseline_tps"])
                ),
                "threshold_tps": 0.8 * float(row["promotion_baseline_tps"]),
                "f3_counterfactual_observed": False,
            }
            for row in rejected
        ],
        "continuation": {
            "minimum_reconnect_gap_seconds": min(reconnect_gaps),
            "median_reconnect_gap_seconds": statistics.median(reconnect_gaps),
            "maximum_reconnect_gap_seconds": max(reconnect_gaps),
        },
        "inference_limits": {
            "seeds": 1,
            "candidate_design": "fixed-scrambled-sobol",
            "rejected_candidate_false_rejection_status": (
                "unknowable-without-forbidden-F3-continuation"
            ),
            "promotion_rate_is_descriptive_only": True,
            "control_p99_is_not_a_promotion_gate": True,
            "does_not_authorize_wave_b_f4_or_apply_best": True,
        },
    }


def analyze_phase_b(settings: Settings, campaign_id: uuid.UUID) -> dict[str, Any]:
    block = _phase_b_block(settings, campaign_id)
    if block["status"] == "ANALYZED":
        analysis = dict(block["analysis"] or {})
        if _canonical_sha256(analysis) != str(block["analysis_sha256"]):
            raise ValueError("Phase B durable analysis hash mismatch")
        return {**analysis, "analysis_sha256": str(block["analysis_sha256"])}
    rows, failed_attempts = _load_authenticated_phase_b_rows(settings, campaign_id)
    _, payload = phase_b_manifest_payload()
    analysis = analyze_phase_b_observations(rows, failed_attempts, payload)
    analysis_sha256 = _canonical_sha256(analysis)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_v2_multifidelity_phase_b_blocks
               SET status='ANALYZED',analysis=%s,analysis_sha256=%s,
                   completed_at=clock_timestamp()
               WHERE phase_b_block_id=%s AND status='OBSERVATIONS_COMPLETE'""",
            (Jsonb(analysis), analysis_sha256, block["phase_b_block_id"]),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Phase B block lost its observations-complete state")
        conn.commit()
    control_campaign(
        settings,
        campaign_id,
        "stop",
        f"Phase B analysis complete: {analysis['decision']}",
        actor="v2-multifidelity-phase-b",
    )
    return {**analysis, "analysis_sha256": analysis_sha256}


def export_phase_b_analysis(
    settings: Settings, campaign_id: uuid.UUID, output_path: Path | None = None
) -> dict[str, Any]:
    block = _phase_b_block(settings, campaign_id)
    if block["status"] != "ANALYZED" or block["analysis"] is None:
        raise ValueError("Phase B export requires a terminal analyzed block")
    artifact_root = (settings.artifact_dir / PHASE_B_STAGE).resolve()
    output = (output_path or artifact_root / "phase-b-analysis.json").resolve()
    if output != artifact_root and artifact_root not in output.parents:
        raise ValueError("Phase B analysis export must stay under its artifact root")
    payload = {
        "campaign_id": str(campaign_id),
        "phase_b_block_id": str(block["phase_b_block_id"]),
        "manifest_sha256": str(block["manifest_sha256"]),
        "phase_a_analysis_sha256": str(block["phase_a_analysis_sha256"]),
        "candidate_design_sha256": str(block["candidate_design_sha256"]),
        "analysis_sha256": str(block["analysis_sha256"]),
        "analysis_sha256_semantics": "canonical JSON analysis payload hash, not file hash",
        "analysis": dict(block["analysis"]),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return {
        "output_path": str(output),
        "file_sha256": _file_sha256(output),
        "analysis_sha256": str(block["analysis_sha256"]),
    }


def phase_b_step_dict(step: PhaseBStep) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(json.dumps(asdict(step), default=str)))
