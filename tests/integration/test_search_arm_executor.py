import hashlib
import json
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from psycopg.errors import RaiseException
from psycopg.types.json import Jsonb

import charmdb.experiment_execution as execution
import charmdb.search_execution as search
from charmdb.arm_restore import ensure_arm_dataset_restored
from charmdb.config import get_settings
from charmdb.db import apply_migrations, connect
from charmdb.manifest_preflight import run_manifest_preflight, validate_dataset_restore

pytestmark = pytest.mark.integration


def execution_definition() -> dict[str, Any]:
    return {
        "dataset": {"snapshot": "executor-fixture-v1", "scale": 10, "schema_sha256": "b" * 64},
        "workload_contexts": [{"label": "transactional", "version": 1}],
        "execution_profile": {
            "warmup_seconds": 0,
            "measurement_seconds": 10,
            "concurrency": 1,
            "failure_limit": 2,
        },
        "search_space": {
            "version": "reload-knobs-v1",
            "parameters": {
                "random_page_cost": {"type": "continuous", "lower": 1.0, "upper": 4.0},
                "work_mem": {
                    "type": "categorical",
                    "values": [1024, 2048, 4096, 8192, 16384, 32768],
                },
                "effective_io_concurrency": {"type": "integer", "lower": 0, "upper": 200},
            },
        },
        "method_parameters": {
            "initial_observations": 4,
            "candidate_pool_size": 16,
            "posterior_samples": 16,
            "max_attempts": 2,
        },
        "resource_limits": {
            "target_cpus": 4,
            "target_memory_bytes": 4_294_967_296,
            "docker_memory_bytes": 8_282_886_144,
        },
        "cache_policy": "integration-test-fixed-block",
        "software_versions": {"charmdb": "0.1.0", "postgresql": "18.1"},
        "budget_accounting": {
            "kind": "F3_EQUIVALENT_WALL_CLOCK",
            "relative_tolerance": 0.05,
            "f3_reference_wall_clock_seconds": 10.0,
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
        "practical_margins": {
            "throughput_relative": 0.05,
            "p99_ms_absolute": 1.0,
            "hypervolume_relative": 0.05,
        },
    }


def _finish_trial(settings: object, trial_id: uuid.UUID, offset: int) -> None:
    started = datetime(2026, 7, 14, 10, offset, tzinfo=UTC)
    completed = started + timedelta(seconds=10)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            """
            UPDATE charm_control.trials
            SET state='COMPLETED',started_at=%s,completed_at=%s,
                objective_values=%s,constraint_values=%s,workflow_result=%s
            WHERE trial_id=%s
            """,
            (
                started,
                completed,
                Jsonb({"throughput_tps": 500.0 + offset, "p99_ms": 12.0}),
                Jsonb({"failures": 0, "p99_margin_ms": 8.0, "feasible": True}),
                Jsonb({"throughput_tps": 500.0 + offset, "p99_ms": 12.0, "failures": 0}),
                trial_id,
            ),
        )
        conn.commit()


def _finish_failed_trial(settings: object, trial_id: uuid.UUID) -> None:
    started = datetime(2026, 7, 14, 10, 30, tzinfo=UTC)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            """UPDATE charm_control.trials
               SET state='CANCELLED',started_at=%s,completed_at=%s,
                   failure_type='INTEGRATION_FIXTURE_FAILURE'
               WHERE trial_id=%s""",
            (started, started + timedelta(milliseconds=500), trial_id),
        )
        conn.commit()


@pytest.mark.skipif(not Path(".env").exists(), reason="integration environment not configured")
def test_search_arm_start_resume_reconciles_without_duplicate_trials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    nonce = uuid.uuid4()
    group_id = uuid.uuid4()
    arm_id = uuid.uuid4()
    group_key = f"integration_search_methods_{nonce.hex}"
    campaign_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:experiment-campaign:{arm_id}")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO charm_control.experiment_groups
            (group_id,group_key,group_kind,hypothesis,baseline,budget_unit,budget_value,seeds,
             required_metrics,prerequisite_gate,status)
            VALUES (%s,%s,'COMPARISON','executor integration only','postgresql_default',
                    'F3-equivalent units per seed',2,%s,%s,'measurement_validity','PLANNED')
            """,
            (group_id, group_key, Jsonb([991]), Jsonb(["throughput_tps", "p99_ms"])),
        )
        cur.execute(
            """
            INSERT INTO charm_control.experiment_arms
            (arm_id,group_id,label,method_definition,random_seed,block_order,budget_value,status)
            VALUES (%s,%s,'postgresql_default',%s,991,0,2,'PLANNED')
            """,
            (arm_id, group_id, Jsonb({"label": "postgresql_default"})),
        )
        conn.commit()
    gate = execution.GateAssessment(
        "measurement_validity",
        True,
        (),
        {
            "measured_f3_trials": 5,
            "active_configuration_matched_f3_trials": 5,
            "artifact_backed_f3_trials": 5,
        },
    )
    original_validate = search._validate_search_arm
    try:
        monkeypatch.setattr(execution, "evaluate_prerequisite_gate", lambda *_args: gate)
        execution.prepare_experiment_arm(
            settings, arm_id, execution_definition(), "integration-test"
        )

        def accept_fixture(arm: dict[str, Any]) -> tuple[search.SearchMethod, dict[str, Any]]:
            patched = {**arm, "group_key": "search_methods"}
            return original_validate(patched)

        monkeypatch.setattr(search, "_validate_search_arm", accept_fixture)
        persisted = search.SearchRecommendation(
            "postgresql_default",
            "DEFAULT",
            0,
            991,
            None,
        )
        search._persist_recommendation(settings, arm_id, persisted)
        with (
            pytest.raises(RaiseException),
            connect(settings.control_dsn) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                """UPDATE charm_control.experiment_search_recommendations
                   SET configuration=%s WHERE arm_id=%s AND budget_position=0""",
                (Jsonb({"tampered": "true"}), arm_id),
            )
        first = search.start_or_resume_search_arm(settings, arm_id, "integration-test")
        repeated = search.start_or_resume_search_arm(settings, arm_id, "integration-test")

        assert first.action == "RECOVERED_UNLINKED_RECOMMENDATION"
        assert repeated.action == "RESUMED_ACTIVE_TRIAL"
        assert repeated.trial_id == first.trial_id
        assert first.trial_id is not None
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) AS count,min(idempotency_key) AS key
                   FROM charm_control.trials WHERE campaign_id=%s""",
                (campaign_id,),
            )
            trial_count = cur.fetchone()
        assert trial_count["count"] == 1
        assert trial_count["key"] == f"experiment:{arm_id}:991:postgresql_default:0"

        _finish_trial(settings, first.trial_id, 0)
        second = search.start_or_resume_search_arm(settings, arm_id, "integration-test")
        assert second.action == "SCHEDULED"
        assert second.trial_id != first.trial_id
        assert second.realized_budget_value == 1.0
        assert second.trial_id is not None

        _finish_failed_trial(settings, second.trial_id)
        third = search.start_or_resume_search_arm(settings, arm_id, "integration-test")
        assert third.action == "SCHEDULED"
        assert third.trial_id is not None
        assert third.realized_budget_value == 1.05

        _finish_trial(settings, third.trial_id, 2)
        final = search.start_or_resume_search_arm(settings, arm_id, "integration-test")
        history = execution.experiment_arm_history(settings, arm_id)
        assert final.action == "FINALIZED"
        assert final.status == "COMPLETED"
        assert final.realized_budget_value == 2.05
        assert len(history["search_recommendations"]) == 3
        assert len(history["budget_entries"]) == 3
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) AS count FROM charm_control.optimization_observations
                   WHERE campaign_id=%s""",
                (campaign_id,),
            )
            assert cur.fetchone()["count"] == 3
    finally:
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """DELETE FROM charm_control.optimization_observations
                   WHERE campaign_id=%s""",
                (campaign_id,),
            )
            cur.execute(
                """DELETE FROM charm_control.experiment_budget_entries
                   WHERE arm_id=%s""",
                (arm_id,),
            )
            cur.execute(
                """DELETE FROM charm_control.experiment_search_recommendations
                   WHERE arm_id=%s""",
                (arm_id,),
            )
            cur.execute(
                """DELETE FROM charm_control.trial_transitions
                   WHERE trial_id IN (
                       SELECT trial_id FROM charm_control.trials WHERE campaign_id=%s
                   )
                """,
                (campaign_id,),
            )
            cur.execute("DELETE FROM charm_control.trials WHERE campaign_id=%s", (campaign_id,))
            cur.execute(
                "DELETE FROM charm_control.experiment_arm_events WHERE arm_id=%s", (arm_id,)
            )
            cur.execute(
                "DELETE FROM charm_control.campaign_events WHERE campaign_id=%s", (campaign_id,)
            )
            cur.execute("DELETE FROM charm_control.experiment_arms WHERE arm_id=%s", (arm_id,))
            cur.execute("DELETE FROM charm_control.campaigns WHERE campaign_id=%s", (campaign_id,))
            cur.execute(
                "DELETE FROM charm_control.experiment_groups WHERE group_id=%s", (group_id,)
            )
            conn.commit()


@pytest.mark.skipif(not Path(".env").exists(), reason="integration environment not configured")
def test_prepared_evidence_arm_restores_with_only_its_own_created_campaign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    nonce = uuid.uuid4()
    group_id = uuid.uuid4()
    arm_id = uuid.uuid4()
    unrelated_campaign_id = uuid.uuid4()
    preflight = None
    campaign_id: uuid.UUID | None = None
    try:
        preflight = run_manifest_preflight(settings)
        definition = execution_definition()
        definition["evidence_lineage"] = {"preflight_id": str(preflight.preflight_id)}
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO charm_control.experiment_groups
                (group_id,group_key,group_kind,hypothesis,baseline,budget_unit,budget_value,seeds,
                 required_metrics,prerequisite_gate,status)
                VALUES (%s,%s,'COMPARISON','restore guard integration only','postgresql_default',
                        'F3-equivalent units per seed',2,%s,%s,'measurement_validity','PLANNED')
                """,
                (
                    group_id,
                    f"integration_restore_guard_{nonce.hex}",
                    Jsonb([992]),
                    Jsonb(["throughput_tps", "p99_ms"]),
                ),
            )
            cur.execute(
                """
                INSERT INTO charm_control.experiment_arms
                (arm_id,group_id,label,method_definition,random_seed,block_order,budget_value,status)
                VALUES (%s,%s,'constrained_bo',%s,992,0,2,'PLANNED')
                """,
                (arm_id, group_id, Jsonb({"label": "constrained_bo"})),
            )
            cur.execute(
                """
                SELECT a.*,g.group_key,g.group_kind,g.hypothesis,g.baseline,g.budget_unit,
                       g.required_metrics,g.prerequisite_gate,g.status AS group_status
                FROM charm_control.experiment_arms a
                JOIN charm_control.experiment_groups g USING(group_id)
                WHERE a.arm_id=%s
                """,
                (arm_id,),
            )
            arm = dict(cur.fetchone() or {})
            frozen = execution._arm_manifest(arm, definition)
            frozen_sha256 = execution.canonical_manifest_sha256(frozen)
            body = (json.dumps(definition, sort_keys=True) + "\n").encode()
            execution_path = preflight.snapshot_path.parent / "integration-execution.json"
            execution_path.write_bytes(body)
            cur.execute(
                """
                INSERT INTO charm_control.experiment_manifest_reviews
                (review_id,preflight_id,arm_id,execution_sha256,frozen_manifest_sha256,
                 execution_relative_path,execution_byte_size,passed,reasons,review)
                VALUES (%s,%s,%s,%s,%s,%s,%s,true,%s,%s)
                """,
                (
                    uuid.uuid4(),
                    preflight.preflight_id,
                    arm_id,
                    hashlib.sha256(body).hexdigest(),
                    frozen_sha256,
                    str(execution_path.relative_to(settings.artifact_dir)),
                    len(body),
                    Jsonb([]),
                    Jsonb({"fixture": "prepared own-campaign restore guard"}),
                ),
            )
            conn.commit()
        gate = execution.GateAssessment(
            "measurement_validity",
            True,
            (),
            {
                "measured_f3_trials": 5,
                "active_configuration_matched_f3_trials": 5,
                "artifact_backed_f3_trials": 5,
            },
        )
        monkeypatch.setattr(execution, "evaluate_prerequisite_gate", lambda *_args: gate)
        prepared = execution.prepare_experiment_arm(
            settings, arm_id, definition, "restore-guard-integration"
        )
        assert prepared.campaign_id is not None
        campaign_id = prepared.campaign_id

        with pytest.raises(RuntimeError, match="active_campaigns"):
            validate_dataset_restore(settings, preflight.preflight_id)

        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO charm_control.campaigns
                (campaign_id,name,mode,status,objective_definition,constraint_definition)
                VALUES (%s,%s,'INTEGRATION','CREATED',%s,%s)
                """,
                (
                    unrelated_campaign_id,
                    f"restore-guard-unrelated-{nonce.hex}",
                    Jsonb({}),
                    Jsonb({}),
                ),
            )
            conn.commit()
        with pytest.raises(RuntimeError, match="active_campaigns"):
            ensure_arm_dataset_restored(
                settings,
                arm_id,
                preflight.preflight_id,
                frozen_sha256,
            )
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM charm_control.campaigns WHERE campaign_id=%s",
                (unrelated_campaign_id,),
            )
            conn.commit()

        first = ensure_arm_dataset_restored(
            settings,
            arm_id,
            preflight.preflight_id,
            frozen_sha256,
        )
        repeated = ensure_arm_dataset_restored(
            settings,
            arm_id,
            preflight.preflight_id,
            frozen_sha256,
        )
        assert first.restored
        assert not repeated.restored
        assert repeated.validation_id == first.validation_id
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) AS count
                   FROM charm_control.experiment_arm_dataset_restores WHERE arm_id=%s""",
                (arm_id,),
            )
            assert cur.fetchone()["count"] == 1
            cur.execute(
                """SELECT count(*) AS count
                   FROM charm_control.experiment_dataset_restore_validations
                   WHERE preflight_id=%s AND passed""",
                (preflight.preflight_id,),
            )
            assert cur.fetchone()["count"] == 1
    finally:
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM charm_control.experiment_arm_dataset_restores WHERE arm_id=%s",
                (arm_id,),
            )
            if preflight is not None:
                cur.execute(
                    """DELETE FROM charm_control.experiment_dataset_restore_validations
                       WHERE preflight_id=%s""",
                    (preflight.preflight_id,),
                )
                cur.execute(
                    "DELETE FROM charm_control.experiment_manifest_reviews WHERE arm_id=%s",
                    (arm_id,),
                )
            cur.execute(
                "DELETE FROM charm_control.experiment_arm_events WHERE arm_id=%s", (arm_id,)
            )
            if campaign_id is not None:
                cur.execute(
                    "DELETE FROM charm_control.campaign_events WHERE campaign_id=%s",
                    (campaign_id,),
                )
            cur.execute("DELETE FROM charm_control.experiment_arms WHERE arm_id=%s", (arm_id,))
            if campaign_id is not None:
                cur.execute(
                    "DELETE FROM charm_control.campaigns WHERE campaign_id=%s", (campaign_id,)
                )
            cur.execute(
                "DELETE FROM charm_control.campaigns WHERE campaign_id=%s",
                (unrelated_campaign_id,),
            )
            cur.execute(
                "DELETE FROM charm_control.experiment_groups WHERE group_id=%s", (group_id,)
            )
            if preflight is not None:
                cur.execute(
                    """DELETE FROM charm_control.experiment_manifest_preflights
                       WHERE preflight_id=%s""",
                    (preflight.preflight_id,),
                )
            conn.commit()
        if preflight is not None:
            shutil.rmtree(preflight.snapshot_path.parent, ignore_errors=True)
