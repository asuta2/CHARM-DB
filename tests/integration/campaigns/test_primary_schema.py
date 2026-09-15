from __future__ import annotations

import os
import uuid

import pytest
from psycopg.errors import RaiseException
from psycopg.types.json import Jsonb

from charmdb.campaigns.default_reference import FROZEN_BASELINE_ID, FROZEN_PREFLIGHT_ID
from charmdb.config import get_settings
from charmdb.db import apply_migrations, connect

pytestmark = pytest.mark.integration


def _configured() -> bool:
    return bool(os.getenv("CHARMDB_DISPOSABLE_INTEGRATION") == "1")


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_primary_and_restore_soak_migrations_enforce_live_transitions() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    campaign_id = uuid.uuid4()
    primary_block_id = uuid.uuid4()
    restore_soak_id = uuid.uuid4()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema='charm_control' AND table_name IN (
                       'experiment_v2_primary_blocks',
                       'experiment_v2_primary_runs',
                       'experiment_v2_primary_attempts',
                       'experiment_v2_primary_training_lineage',
                       'experiment_v2_primary_restore_soaks',
                       'experiment_v2_primary_restore_soak_runs'
                   )"""
            )
            tables = {str(row["table_name"]) for row in cur.fetchall()}
            assert len(tables) == 6

            cur.execute(
                """SELECT trigger_name FROM information_schema.triggers
                   WHERE event_object_schema='charm_control' AND event_object_table IN (
                       'experiment_v2_primary_blocks',
                       'experiment_v2_primary_runs',
                       'experiment_v2_primary_attempts',
                       'experiment_v2_primary_training_lineage',
                       'experiment_v2_primary_restore_soaks',
                       'experiment_v2_primary_restore_soak_runs'
                   )"""
            )
            triggers = {str(row["trigger_name"]) for row in cur.fetchall()}
            assert {
                "v2_primary_block_transition_guard",
                "v2_primary_run_transition_guard",
                "v2_primary_attempt_transition_guard",
                "v2_primary_training_lineage_guard",
                "v2_primary_restore_soak_transition_guard",
                "v2_primary_restore_soak_run_transition_guard",
            } <= triggers

            cur.execute(
                """INSERT INTO charm_control.campaigns
                   (campaign_id,name,mode,status,objective_definition,
                    constraint_definition,settings,failure_limit)
                   VALUES (%s,%s,'PRIMARY','CREATED',%s,%s,%s,393)""",
                (
                    campaign_id,
                    f"primary-schema-integration-{campaign_id}",
                    Jsonb({"maximize": "throughput_tps", "minimize": "p99_ms"}),
                    Jsonb({"hard_gates": True}),
                    Jsonb({"integration_fixture": True}),
                ),
            )
            cur.execute(
                """INSERT INTO charm_control.experiment_v2_primary_blocks
                   (primary_block_id,campaign_id,protocol_id,evidence_role,
                    manifest_sha256,preflight_id,baseline_id,benchmark_profile_id,
                    schedule_sha256,candidate_design_sha256,status,retry_policy,
                    drift_interpretation)
                   VALUES (%s,%s,'thesis-protocol-v2','PRIMARY',%s,%s,%s,%s,%s,%s,
                           'PLANNED',%s,%s)""",
                (
                    primary_block_id,
                    campaign_id,
                    "a" * 64,
                    FROZEN_PREFLIGHT_ID,
                    FROZEN_BASELINE_ID,
                    "scale500-c32-w600-f3-600-v1",
                    "b" * 64,
                    "c" * 64,
                    Jsonb({"maximum_trials_per_slot": 3}),
                    Jsonb({"flag_is_campaign_killing": False}),
                ),
            )
            cur.execute(
                """UPDATE charm_control.experiment_v2_primary_blocks
                   SET status='RUNNING' WHERE primary_block_id=%s""",
                (primary_block_id,),
            )
            with (
                pytest.raises(RaiseException, match="invalid v2 primary block transition"),
                conn.transaction(),
            ):
                cur.execute(
                    """UPDATE charm_control.experiment_v2_primary_blocks
                       SET status='PLANNED' WHERE primary_block_id=%s""",
                    (primary_block_id,),
                )

            cur.execute(
                """INSERT INTO charm_control.experiment_v2_primary_restore_soaks
                   (restore_soak_id,protocol_id,evidence_role,manifest_sha256,
                    contract_sha256,preflight_id,baseline_id,restore_mechanism,
                    required_repetitions,artifact_directory,status)
                   VALUES (%s,'thesis-protocol-v2','INFRASTRUCTURE',%s,%s,%s,%s,
                           'logical-restore',15,%s,'PLANNED')""",
                (
                    restore_soak_id,
                    "d" * 64,
                    "e" * 64,
                    FROZEN_PREFLIGHT_ID,
                    FROZEN_BASELINE_ID,
                    str(settings.artifact_dir.resolve()),
                ),
            )
            cur.execute(
                """UPDATE charm_control.experiment_v2_primary_restore_soaks
                   SET status='RUNNING' WHERE restore_soak_id=%s""",
                (restore_soak_id,),
            )
            with (
                pytest.raises(RaiseException, match="invalid v2 primary restore-soak"),
                conn.transaction(),
            ):
                cur.execute(
                    """UPDATE charm_control.experiment_v2_primary_restore_soaks
                       SET status='PLANNED' WHERE restore_soak_id=%s""",
                    (restore_soak_id,),
                )
        finally:
            conn.rollback()
