from __future__ import annotations

import os
import uuid

import pytest
from psycopg.errors import CheckViolation, RaiseException
from psycopg.types.json import Jsonb

from charmdb.campaigns.default_reference import FROZEN_BASELINE_ID, FROZEN_PREFLIGHT_ID
from charmdb.config import get_settings
from charmdb.db import apply_migrations, connect

pytestmark = pytest.mark.integration

CHAMPION_SOURCE = uuid.UUID("e6d70fab-0c90-5795-96a1-5021bff1194c")
CHAMPION_CONFIGURATION = {
    "checkpoint_completion_target": "0.936696",
    "checkpoint_timeout": "1430",
    "effective_cache_size": "465506",
    "max_parallel_workers_per_gather": "3",
    "max_wal_size": "633",
    "random_page_cost": "3.655316",
    "shared_buffers": "182848",
    "work_mem": "18022",
}
RUN_INSERT = """INSERT INTO charm_control.experiment_v2_h25_runs
    (h25_run_id,h25_block_id,campaign_id,physical_position,repetition_block,
     within_block_position,treatment,source_primary_run_id,random_seed,
     requested_configuration,proposal_sha256,status)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1076286005,%s,%s,'PROPOSED')"""


def _configured() -> bool:
    return bool(os.getenv("CHARMDB_DISPOSABLE_INTEGRATION") == "1")


def _insert_run(cur, block_id, campaign_id, row) -> None:  # type: ignore[no-untyped-def]
    position, block, within, treatment, source, configuration = row
    cur.execute(
        RUN_INSERT,
        (
            uuid.uuid4(),
            block_id,
            campaign_id,
            position,
            block,
            within,
            treatment,
            source,
            Jsonb(configuration),
            "f" * 64,
        ),
    )


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_h25_migrations_enforce_forward_only_transitions_and_three_arm_layout() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    f4_campaign_id = uuid.uuid4()
    f4_block_id = uuid.uuid4()
    campaign_id = uuid.uuid4()
    block_id = uuid.uuid4()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema='charm_control' AND table_name IN (
                       'experiment_v2_h25_blocks','experiment_v2_h25_runs',
                       'experiment_v2_h25_attempts')"""
            )
            assert len(cur.fetchall()) == 3
            for identifier, mode in ((f4_campaign_id, "F4_CONFIRMATION"), (campaign_id, "H25")):
                cur.execute(
                    """INSERT INTO charm_control.campaigns
                       (campaign_id,name,mode,status,objective_definition,
                        constraint_definition,settings,failure_limit)
                       VALUES (%s,%s,%s,'CREATED',%s,%s,%s,12)""",
                    (
                        identifier,
                        f"h25-schema-integration-{identifier}",
                        mode,
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
                    f4_block_id,
                    f4_campaign_id,
                    "a" * 64,
                    "b" * 64,
                    "c" * 64,
                    FROZEN_PREFLIGHT_ID,
                    FROZEN_BASELINE_ID,
                    "scale500-c32-w600-f4-600-v1",
                ),
            )
            cur.execute(
                """INSERT INTO charm_control.experiment_v2_h25_blocks
                   (h25_block_id,campaign_id,protocol_id,evidence_role,manifest_sha256,
                    f4_campaign_id,f4_block_id,f4_manifest_sha256,f4_analysis_sha256,
                    preflight_id,baseline_id,benchmark_profile_id,design_kind,status)
                   VALUES (%s,%s,'thesis-protocol-v2','SECONDARY',%s,%s,%s,%s,%s,%s,%s,%s,
                           'three-arm-heuristic-default-champion','PLANNED')""",
                (
                    block_id,
                    campaign_id,
                    "d" * 64,
                    f4_campaign_id,
                    f4_block_id,
                    "a" * 64,
                    "e" * 64,
                    FROZEN_PREFLIGHT_ID,
                    FROZEN_BASELINE_ID,
                    "scale500-c32-w600-h25-600-v1",
                ),
            )
            cur.execute(
                "UPDATE charm_control.experiment_v2_h25_blocks SET status='RUNNING' "
                "WHERE h25_block_id=%s",
                (block_id,),
            )
            with (
                pytest.raises(RaiseException, match="invalid v2 H25 block transition"),
                conn.transaction(),
            ):
                cur.execute(
                    "UPDATE charm_control.experiment_v2_h25_blocks SET status='PLANNED' "
                    "WHERE h25_block_id=%s",
                    (block_id,),
                )
            for row in (
                (1, 1, 1, "H25", None, {"shared_buffers": "131072"}),
                (2, 1, 2, "DEFAULT", None, {"shared_buffers": "16384"}),
                (3, 1, 3, "E", CHAMPION_SOURCE, CHAMPION_CONFIGURATION),
            ):
                _insert_run(cur, block_id, campaign_id, row)
            check_violations = (
                (4, 2, 1, "H25", None, {"shared_buffers": "16384"}),
                (6, 2, 3, "E", None, CHAMPION_CONFIGURATION),
                (6, 2, 3, "E", CHAMPION_SOURCE, {**CHAMPION_CONFIGURATION, "work_mem": "1024"}),
                (5, 2, 2, "DEFAULT", None, {"shared_buffers": "131072"}),
            )
            for row in check_violations:
                with pytest.raises(CheckViolation), conn.transaction():
                    _insert_run(cur, block_id, campaign_id, row)
            layout_violations = (
                ((4, 2, 1, "DEFAULT", None, {"shared_buffers": "16384"}), "center position"),
                ((5, 2, 2, "H25", None, {"shared_buffers": "131072"}), "outer position"),
                ((13, 2, 3, "E", CHAMPION_SOURCE, CHAMPION_CONFIGURATION), "inconsistent"),
            )
            for row, message in layout_violations:
                with pytest.raises(RaiseException, match=message), conn.transaction():
                    _insert_run(cur, block_id, campaign_id, row)
        finally:
            conn.rollback()
