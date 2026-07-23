import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from psycopg.errors import RaiseException
from psycopg.types.json import Jsonb

import charmdb.experiment_execution as execution
from charmdb.config import get_settings
from charmdb.db import connect

pytestmark = pytest.mark.integration


def execution_definition() -> dict[str, Any]:
    return {
        "dataset": {"snapshot": "fixture-v1", "scale": 10, "schema_sha256": "a" * 64},
        "workload_contexts": [{"label": "transactional", "version": 1}],
        "execution_profile": {
            "warmup_seconds": 2,
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
            "candidate_pool_size": 2048,
            "posterior_samples": 256,
            "max_attempts": 3,
        },
        "resource_limits": {
            "target_cpus": 4,
            "target_memory_bytes": 4_294_967_296,
            "docker_memory_bytes": 8_282_886_144,
        },
        "cache_policy": "integration-test-block",
        "software_versions": {"charmdb": "0.1.0", "postgresql": "18.1"},
        "budget_accounting": {
            "kind": "F3_EQUIVALENT_WALL_CLOCK",
            "relative_tolerance": 0.0,
            "f3_reference_wall_clock_seconds": 10.0,
        },
        "objective_definition": {
            "maximize": ["throughput_tps", "negative_p99_ms"],
            "reference_point": [0.0, -20.0],
        },
        "constraint_definition": {"p99_ms_max": 20.0, "failures_max": 0},
        "practical_margins": {
            "throughput_relative": 0.05,
            "p99_ms_absolute": 1.0,
            "hypervolume_relative": 0.05,
        },
    }


@pytest.mark.skipif(not Path(".env").exists(), reason="integration environment not configured")
def test_experiment_arm_lifecycle_is_idempotent_and_budget_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    nonce = uuid.uuid4()
    group_id = uuid.uuid4()
    arm_id = uuid.uuid4()
    blocked_arm_id = uuid.uuid4()
    trial_id = uuid.uuid4()
    campaign_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:experiment-campaign:{arm_id}")
    group_key = f"integration_lifecycle_{nonce.hex}"
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.experiment_groups
               (group_id,group_key,group_kind,hypothesis,baseline,budget_unit,budget_value,
                seeds,required_metrics,prerequisite_gate,status)
               VALUES (%s,%s,'COMPARISON','integration lifecycle only','baseline',
                       'F3-equivalent units per seed',1,%s,%s,'measurement_validity','PLANNED')""",
            (group_id, group_key, Jsonb([991]), Jsonb(["throughput_tps", "p99_ms"])),
        )
        cur.execute(
            """INSERT INTO charm_control.experiment_arms
               (arm_id,group_id,label,method_definition,random_seed,block_order,budget_value,status)
               VALUES (%s,%s,'baseline',%s,991,0,1,'PLANNED')""",
            (arm_id, group_id, Jsonb({"label": "baseline", "hard_safety_always_active": True})),
        )
        cur.execute(
            """INSERT INTO charm_control.experiment_arms
               (arm_id,group_id,label,method_definition,random_seed,block_order,budget_value,status)
               VALUES (%s,%s,'blocked',%s,991,1,1,'PLANNED')""",
            (
                blocked_arm_id,
                group_id,
                Jsonb({"label": "blocked", "hard_safety_always_active": True}),
            ),
        )
        conn.commit()

    blocked_assessment = execution.GateAssessment(
        "measurement_validity", False, ("integration closed gate",), {"measured_f3_trials": 0}
    )
    assessment = execution.GateAssessment(
        "measurement_validity",
        True,
        (),
        {
            "measured_f3_trials": 5,
            "active_configuration_matched_f3_trials": 5,
            "artifact_backed_f3_trials": 5,
        },
    )
    definition = execution_definition()
    started = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    completed = started + timedelta(seconds=10)
    try:
        monkeypatch.setattr(
            execution,
            "evaluate_prerequisite_gate",
            lambda _settings, _gate: blocked_assessment,
        )
        blocked = execution.prepare_experiment_arm(
            settings, blocked_arm_id, definition, "integration-test"
        )
        blocked_history = execution.experiment_arm_history(settings, blocked_arm_id)
        assert blocked.status == "BLOCKED"
        assert blocked.campaign_id is None
        assert blocked_history["arm"]["execution_manifest"] is None
        assert blocked_history["events"][0]["event_type"] == "GATE_BLOCKED"

        monkeypatch.setattr(
            execution, "evaluate_prerequisite_gate", lambda _settings, _gate: assessment
        )
        first = execution.prepare_experiment_arm(settings, arm_id, definition, "integration-test")
        repeated = execution.prepare_experiment_arm(
            settings, arm_id, definition, "integration-test"
        )

        assert first == repeated
        assert first.campaign_id == campaign_id
        assert first.status == "PREPARED"

        with (
            pytest.raises(RaiseException, match="immutable"),
            connect(settings.control_dsn) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                """UPDATE charm_control.experiment_arms
                   SET execution_manifest=jsonb_set(
                       execution_manifest,'{execution,cache_policy}','\"changed\"'::jsonb
                   ) WHERE arm_id=%s""",
                (arm_id,),
            )

        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO charm_control.trials
                   (trial_id,campaign_id,state,benchmark_profile,fidelity,random_seed,workflow_kind,
                    workflow_payload,started_at,completed_at)
                   VALUES (%s,%s,'COMPLETED','integration-lifecycle',3,991,
                           'BASELINE_BENCHMARK','{}'::jsonb,%s,%s)""",
                (trial_id, campaign_id, started, completed),
            )
            conn.commit()

        with pytest.raises(ValueError, match="below target"):
            execution.finalize_experiment_arm(settings, arm_id, "integration-test")

        charged = execution.record_experiment_trial_budget(
            settings, arm_id, trial_id, actor="integration-test"
        )
        repeated_charge = execution.record_experiment_trial_budget(
            settings, arm_id, trial_id, actor="integration-test"
        )
        finalized = execution.finalize_experiment_arm(settings, arm_id, "integration-test")
        history = execution.experiment_arm_history(settings, arm_id)

        assert charged["inserted"] is True
        assert repeated_charge["inserted"] is False
        assert charged["realized_budget_value"] == 1.0
        assert finalized["status"] == "COMPLETED"
        assert history["arm"]["manifest_sha256"] == first.manifest_sha256
        assert [item["event_type"] for item in history["events"]] == [
            "MANIFEST_FROZEN",
            "PREPARED",
            "BUDGET_RECORDED",
            "COMPLETED",
        ]
    finally:
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM charm_control.experiment_budget_entries WHERE arm_id=%s", (arm_id,)
            )
            cur.execute(
                "DELETE FROM charm_control.experiment_arm_events WHERE arm_id=ANY(%s)",
                ([arm_id, blocked_arm_id],),
            )
            cur.execute("DELETE FROM charm_control.trials WHERE trial_id=%s", (trial_id,))
            cur.execute(
                "DELETE FROM charm_control.campaign_events WHERE campaign_id=%s", (campaign_id,)
            )
            cur.execute(
                "DELETE FROM charm_control.experiment_arms WHERE arm_id=ANY(%s)",
                ([arm_id, blocked_arm_id],),
            )
            cur.execute("DELETE FROM charm_control.campaigns WHERE campaign_id=%s", (campaign_id,))
            cur.execute(
                "DELETE FROM charm_control.experiment_groups WHERE group_id=%s", (group_id,)
            )
            conn.commit()
