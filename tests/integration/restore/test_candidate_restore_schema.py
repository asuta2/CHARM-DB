from __future__ import annotations

import os
import uuid

import pytest

from charmdb.campaigns.saturation_phase2 import phase2_readiness
from charmdb.config import get_settings
from charmdb.db import apply_migrations, connect

pytestmark = pytest.mark.integration


def _configured() -> bool:
    return bool(os.getenv("CHARMDB_DISPOSABLE_INTEGRATION") == "1")


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_candidate_restore_migration_exposes_immutable_ledger_contract() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='charm_control'
                 AND table_name='experiment_candidate_dataset_restores'"""
        )
        columns = {str(row["column_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT trigger_name FROM information_schema.triggers
               WHERE event_object_schema='charm_control'
                 AND event_object_table IN (
                     'experiment_candidate_dataset_restores',
                     'experiment_candidate_dataset_baselines'
                 )"""
        )
        triggers = {str(row["trigger_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='charm_control' AND table_name='trials'
                 AND column_name IN (
                     'protocol_id','evidence_role','evaluation_role',
                     'candidate_restore_required','candidate_dataset_restore_id'
                 )"""
        )
        trial_columns = {str(row["column_name"]) for row in cur.fetchall()}

    assert {
        "restore_id",
        "trial_id",
        "baseline_id",
        "pre_restore_fingerprint",
        "post_restore_fingerprint",
        "retry_of_restore_id",
        "recovery_root_id",
        "duration_seconds",
        "exact_core_passed",
        "physical_statistics_passed",
    } <= columns
    assert {
        "candidate_dataset_restore_transition_guard",
        "candidate_dataset_baseline_immutable",
    } <= triggers
    assert trial_columns == {
        "protocol_id",
        "evidence_role",
        "evaluation_role",
        "candidate_restore_required",
        "candidate_dataset_restore_id",
    }


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_restore_capability_and_physical_archive_schema_is_persisted() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema='charm_control'
                 AND table_name IN (
                    'experiment_restore_capability_assessments',
                    'experiment_physical_dataset_archives'
                 )"""
        )
        tables = {str(row["table_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT trigger_name FROM information_schema.triggers
               WHERE event_object_schema='charm_control'
                 AND event_object_table IN (
                    'experiment_restore_capability_assessments',
                    'experiment_physical_dataset_archives'
                 )"""
        )
        triggers = {str(row["trigger_name"]) for row in cur.fetchall()}

    assert tables == {
        "experiment_restore_capability_assessments",
        "experiment_physical_dataset_archives",
    }
    assert {
        "restore_capability_immutable",
        "physical_archive_transition_guard",
        "physical_archive_delete_guard",
    } <= triggers


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_saturation_phase1_schema_persists_ladder_and_probe_contract() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema='charm_control'
                 AND table_name IN (
                    'experiment_saturation_phase1_ladders',
                    'experiment_saturation_phase1_probes'
                 )"""
        )
        tables = {str(row["table_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT trigger_name FROM information_schema.triggers
               WHERE event_object_schema='charm_control'
                 AND event_object_table IN (
                    'experiment_saturation_phase1_ladders',
                    'experiment_saturation_phase1_probes'
                 )"""
        )
        triggers = {str(row["trigger_name"]) for row in cur.fetchall()}

    assert tables == {
        "experiment_saturation_phase1_ladders",
        "experiment_saturation_phase1_probes",
    }
    assert {
        "saturation_phase1_ladder_transition_guard",
        "saturation_phase1_probe_transition_guard",
    } <= triggers


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_saturation_phase2_schema_persists_configuration_contract() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema='charm_control'
                 AND table_name='experiment_saturation_phase2_configurations'"""
        )
        tables = {str(row["table_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT trigger_name FROM information_schema.triggers
               WHERE event_object_schema='charm_control'
                 AND event_object_table='experiment_saturation_phase2_configurations'"""
        )
        triggers = {str(row["trigger_name"]) for row in cur.fetchall()}

    assert tables == {"experiment_saturation_phase2_configurations"}
    assert "saturation_phase2_transition_guard" in triggers


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_warmup_duration_schema_persists_two_stage_gate() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema='charm_control'
                 AND table_name IN (
                    'experiment_warmup_duration_pilots',
                    'experiment_warmup_duration_runs'
                 )"""
        )
        tables = {str(row["table_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT trigger_name FROM information_schema.triggers
               WHERE event_object_schema='charm_control'
                 AND event_object_table IN (
                    'experiment_warmup_duration_pilots',
                    'experiment_warmup_duration_runs'
                 )"""
        )
        triggers = {str(row["trigger_name"]) for row in cur.fetchall()}

    assert tables == {
        "experiment_warmup_duration_pilots",
        "experiment_warmup_duration_runs",
    }
    assert {
        "warmup_duration_pilot_transition_guard",
        "warmup_duration_run_transition_guard",
    } <= triggers


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_v2_default_reference_schema_persists_block_and_chronology_contract() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema='charm_control'
                 AND table_name IN (
                    'experiment_v2_default_reference_blocks',
                    'experiment_v2_default_reference_runs'
                 )"""
        )
        tables = {str(row["table_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT trigger_name FROM information_schema.triggers
               WHERE event_object_schema='charm_control'
                 AND event_object_table IN (
                    'experiment_v2_default_reference_blocks',
                    'experiment_v2_default_reference_runs'
                 )"""
        )
        triggers = {str(row["trigger_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='charm_control'
                 AND table_name='experiment_v2_default_reference_runs'
                 AND column_name IN (
                    'chronological_execution_index','subphase','random_seed','trial_id'
                 )"""
        )
        columns = {str(row["column_name"]) for row in cur.fetchall()}

    assert tables == {
        "experiment_v2_default_reference_blocks",
        "experiment_v2_default_reference_runs",
    }
    assert {
        "v2_default_reference_block_transition_guard",
        "v2_default_reference_run_transition_guard",
    } <= triggers
    assert columns == {
        "chronological_execution_index",
        "subphase",
        "random_seed",
        "trial_id",
    }


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_post_screening_temporal_stability_schema_persists_source_lineage() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='charm_control'
                 AND table_name='experiment_v2_default_reference_blocks'
                 AND column_name IN (
                    'source_screening_recovery_block_id','qualification_purpose'
                 )"""
        )
        columns = {str(row["column_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT indexname FROM pg_indexes
               WHERE schemaname='charm_control'
                 AND tablename='experiment_v2_default_reference_blocks'
                 AND indexname='v2_default_reference_temporal_source_unique'"""
        )
        indexes = {str(row["indexname"]) for row in cur.fetchall()}

    assert columns == {
        "source_screening_recovery_block_id",
        "qualification_purpose",
    }
    assert indexes == {"v2_default_reference_temporal_source_unique"}


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_v2_screening_schema_persists_schedule_and_analysis_contract() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema='charm_control'
                 AND table_name IN (
                    'experiment_v2_screening_blocks',
                    'experiment_v2_screening_runs'
                 )"""
        )
        tables = {str(row["table_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT trigger_name FROM information_schema.triggers
               WHERE event_object_schema='charm_control'
                 AND event_object_table IN (
                    'experiment_v2_screening_blocks',
                    'experiment_v2_screening_runs'
                 )"""
        )
        triggers = {str(row["trigger_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='charm_control'
                 AND table_name='experiment_v2_screening_runs'
                 AND column_name IN (
                    'chronological_execution_index','phase','evaluation_kind',
                    'sobol_index','oat_parameter','requested_configuration','trial_id'
                 )"""
        )
        columns = {str(row["column_name"]) for row in cur.fetchall()}

    assert tables == {
        "experiment_v2_screening_blocks",
        "experiment_v2_screening_runs",
    }
    assert {
        "v2_screening_block_transition_guard",
        "v2_screening_run_transition_guard",
    } <= triggers
    assert columns == {
        "chronological_execution_index",
        "phase",
        "evaluation_kind",
        "sobol_index",
        "oat_parameter",
        "requested_configuration",
        "trial_id",
    }


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_v2_screening_recovery_schema_persists_validation_and_lineage_contract() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema='charm_control'
                 AND table_name IN (
                    'experiment_v2_restore_stability_blocks',
                    'experiment_v2_restore_stability_runs',
                    'experiment_v2_screening_recovery_blocks',
                    'experiment_v2_screening_recovery_runs'
                 )"""
        )
        tables = {str(row["table_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT trigger_name FROM information_schema.triggers
               WHERE event_object_schema='charm_control'
                 AND event_object_table IN (
                    'experiment_v2_restore_stability_blocks',
                    'experiment_v2_restore_stability_runs',
                    'experiment_v2_screening_recovery_blocks',
                    'experiment_v2_screening_recovery_runs'
                 )"""
        )
        triggers = {str(row["trigger_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='charm_control'
                 AND table_name='experiment_v2_screening_recovery_runs'
                 AND column_name IN (
                    'source_run_id','chronological_execution_index','phase',
                    'evaluation_kind','requested_configuration','trial_id'
                 )"""
        )
        columns = {str(row["column_name"]) for row in cur.fetchall()}

    assert tables == {
        "experiment_v2_restore_stability_blocks",
        "experiment_v2_restore_stability_runs",
        "experiment_v2_screening_recovery_blocks",
        "experiment_v2_screening_recovery_runs",
    }
    assert {
        "v2_restore_stability_block_transition_guard",
        "v2_restore_stability_run_transition_guard",
        "v2_screening_recovery_block_transition_guard",
        "v2_screening_recovery_run_transition_guard",
    } <= triggers
    assert columns == {
        "source_run_id",
        "chronological_execution_index",
        "phase",
        "evaluation_kind",
        "requested_configuration",
        "trial_id",
    }


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_v2_screening_recovery_remediation_schema_is_forward_only_and_immutable() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='charm_control'
                 AND table_name='experiment_v2_target_database_remediations'"""
        )
        remediation_columns = {str(row["column_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema='charm_control'
                 AND table_name='experiment_v2_restore_stability_blocks'
                 AND column_name IN ('remediation_id','supersedes_validation_block_id')"""
        )
        validation_columns = {str(row["column_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT trigger_name FROM information_schema.triggers
               WHERE event_object_schema='charm_control'
                 AND event_object_table='experiment_v2_target_database_remediations'"""
        )
        triggers = {str(row["trigger_name"]) for row in cur.fetchall()}
        cur.execute(
            """SELECT count(*) AS count FROM pg_constraint c
               JOIN pg_class t ON t.oid=c.conrelid
               JOIN pg_namespace n ON n.oid=t.relnamespace
               WHERE n.nspname='charm_control'
                 AND t.relname='experiment_v2_restore_stability_blocks'
                 AND c.contype='u'
                 AND pg_get_constraintdef(c.oid)='UNIQUE (source_screening_block_id)'"""
        )
        source_only_unique_constraints = int(cur.fetchone()["count"])

    assert {
        "remediation_id",
        "failed_validation_block_id",
        "manifest_sha256",
        "remediation_sha256",
        "pre_evidence",
        "post_evidence",
        "result_sha256",
    } <= remediation_columns
    assert validation_columns == {"remediation_id", "supersedes_validation_block_id"}
    assert "v2_target_database_remediation_transition_guard" in triggers
    assert source_only_unique_constraints == 0


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_saturation_phase2_readiness_matches_approved_scale500_baseline() -> None:
    settings = get_settings()
    baseline_id = uuid.UUID("35459275-c9bf-544f-be54-4e3537464c78")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM charm_control.experiment_candidate_dataset_baselines
               WHERE baseline_id=%s AND approved""",
            (baseline_id,),
        )
        if cur.fetchone() is None:
            pytest.skip("selected scale-500 baseline is not installed")
    report = phase2_readiness(
        settings,
        uuid.UUID("137d0027-c256-59fa-b583-4277e6461486"),
    )

    assert report["ready"] is False
    assert report["phase2_status"] == "passed"
    assert report["baseline_id"] == "35459275-c9bf-544f-be54-4e3537464c78"
    assert report["configuration_count"] == 8
    assert report["knob_count"] == 11
