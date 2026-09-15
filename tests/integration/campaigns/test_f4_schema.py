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
def test_f4_migration_enforces_forward_only_block_transition() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    campaign_id = uuid.uuid4()
    block_id = uuid.uuid4()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema='charm_control' AND table_name IN (
                       'experiment_v2_f4_blocks','experiment_v2_f4_runs',
                       'experiment_v2_f4_attempts')"""
            )
            assert len(cur.fetchall()) == 3
            cur.execute(
                """INSERT INTO charm_control.campaigns
                   (campaign_id,name,mode,status,objective_definition,
                    constraint_definition,settings,failure_limit)
                   VALUES (%s,%s,'F4_CONFIRMATION','CREATED',%s,%s,%s,20)""",
                (
                    campaign_id,
                    f"f4-schema-integration-{campaign_id}",
                    Jsonb({"maximize": "throughput_tps", "minimize": "p99_ms"}),
                    Jsonb({"hard_gates": True}),
                    Jsonb({"integration_fixture": True}),
                ),
            )
            cur.execute(
                """INSERT INTO charm_control.experiment_v2_f4_blocks
                   (f4_block_id,campaign_id,protocol_id,evidence_role,manifest_sha256,
                    primary_analysis_sha256,pareto_table_sha256,preflight_id,baseline_id,
                    benchmark_profile_id,status)
                   VALUES (
                       %s,%s,'thesis-protocol-v2','F4_CONFIRMATION',%s,%s,%s,%s,%s,%s,'PLANNED'
                   )""",
                (
                    block_id,
                    campaign_id,
                    "a" * 64,
                    "b" * 64,
                    "c" * 64,
                    FROZEN_PREFLIGHT_ID,
                    FROZEN_BASELINE_ID,
                    "scale500-c32-w600-f4-600-v1",
                ),
            )
            cur.execute(
                "UPDATE charm_control.experiment_v2_f4_blocks SET status='RUNNING' "
                "WHERE f4_block_id=%s",
                (block_id,),
            )
            with (
                pytest.raises(RaiseException, match="invalid v2 F4 block transition"),
                conn.transaction(),
            ):
                cur.execute(
                    "UPDATE charm_control.experiment_v2_f4_blocks SET status='PLANNED' "
                    "WHERE f4_block_id=%s",
                    (block_id,),
                )
        finally:
            conn.rollback()
