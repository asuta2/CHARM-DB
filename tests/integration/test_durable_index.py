import hashlib
import os
import uuid
from pathlib import Path

import pytest

import charmdb.worker as worker
from charmdb.config import get_settings
from charmdb.db import apply_migrations, connect
from charmdb.indexing import (
    load_candidate,
    seed_index_fixture,
    verify_index_absent,
    verify_index_active,
)
from charmdb.worker import (
    control_campaign,
    create_campaign,
    create_index_lifecycle_trial,
    run_once,
    trial_history,
)

pytestmark = pytest.mark.integration


def _configured() -> bool:
    return bool(os.getenv("CHARMDB_TARGET_DSN") or Path(".env").exists())


def _expire_lease(trial_id: uuid.UUID) -> None:
    settings = get_settings()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.trials
            SET lease_expires_at=clock_timestamp() - interval '1 second'
            WHERE trial_id=%s""",
            (trial_id,),
        )
        conn.commit()


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_index_build_and_drop_ambiguity_reconcile_without_duplicate_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    seed_index_fixture(settings, rows=10_000)
    campaign_id = create_campaign(
        settings,
        f"durable-index-recovery-{uuid.uuid4().hex[:8]}",
        "INDEX_RELIABILITY",
        {"validate": "managed_index_lifecycle"},
        {"managed_only": True, "final_index_absent": True},
        actor="pytest",
    )
    control_campaign(settings, campaign_id, "resume", "start index recovery", "pytest")
    trial_id = create_index_lifecycle_trial(
        settings,
        campaign_id,
        "public",
        "charm_index_fixture",
        ("customer_id",),
        ("id", "amount"),
        20260724,
        f"durable-index-recovery-{uuid.uuid4()}",
        max_attempts=3,
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT requested_configuration->>'index_candidate_id' AS candidate_id "
            "FROM charm_control.trials WHERE trial_id=%s",
            (trial_id,),
        )
        candidate_id = uuid.UUID(str(cur.fetchone()["candidate_id"]))  # type: ignore[index]
    candidate = load_candidate(settings, candidate_id)
    original_complete = worker._record_action_complete
    interrupted_build = False
    interrupted_drop = False

    def terminate_after_catalog_mutation(
        _settings_arg: object,
        lease: worker.TrialLease,
        state: str,
        result: dict[str, object],
    ) -> None:
        nonlocal interrupted_build, interrupted_drop
        if state == "BUILDING_INDEXES" and not interrupted_build:
            interrupted_build = True
            raise SystemExit("controlled death after index build commit")
        if state == "SELECTING_NEXT_ACTION" and not interrupted_drop:
            interrupted_drop = True
            raise SystemExit("controlled death after index drop commit")
        original_complete(settings, lease, state, result)

    monkeypatch.setattr(worker, "_record_action_complete", terminate_after_catalog_mutation)
    with pytest.raises(SystemExit, match="after index build commit"):
        run_once(
            settings,
            owner="index-worker-build",
            lease_seconds=30,
            campaign_id=campaign_id,
        )
    verify_index_active(settings, candidate)
    _expire_lease(trial_id)

    with pytest.raises(SystemExit, match="after index drop commit"):
        run_once(
            settings,
            owner="index-worker-drop",
            lease_seconds=30,
            campaign_id=campaign_id,
        )
    verify_index_absent(settings, candidate)
    _expire_lease(trial_id)

    recovered = run_once(
        settings,
        owner="index-worker-final",
        lease_seconds=30,
        campaign_id=campaign_id,
    )
    assert recovered.recovered_stale_lease
    assert recovered.state == "COMPLETED"
    verify_index_absent(settings, candidate)
    history = trial_history(settings, trial_id)
    assert history["attempt_count"] == 3
    build_action = next(
        action for action in history["actions"] if action["state"] == "BUILDING_INDEXES"
    )
    drop_action = next(
        action for action in history["actions"] if action["state"] == "SELECTING_NEXT_ACTION"
    )
    assert build_action["attempt"] == 2
    assert build_action["result"]["recovered_existing"] is True
    assert build_action["result"]["measurement_complete"] is True
    assert drop_action["attempt"] == 3
    assert drop_action["result"]["drop"]["recovered_absent"] is True
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT to_state,count(*) AS count
            FROM charm_control.index_lifecycle_events
            WHERE candidate_id=%s AND to_state IN ('BUILDING','ACTIVE','DROPPING','DROPPED')
              AND event_id >= (
                  SELECT max(event_id) FROM charm_control.index_lifecycle_events
                  WHERE candidate_id=%s AND to_state='PROPOSED'
              )
            GROUP BY to_state""",
            (candidate_id, candidate_id),
        )
        counts = {str(row["to_state"]): int(row["count"]) for row in cur.fetchall()}
        assert counts == {"BUILDING": 1, "ACTIVE": 1, "DROPPING": 1, "DROPPED": 1}
        cur.execute(
            """SELECT relative_path,sha256,byte_size FROM charm_control.artifacts
            WHERE trial_id=%s AND kind='durable-index-lifecycle-json'""",
            (trial_id,),
        )
        artifact_row = cur.fetchone()
        assert artifact_row is not None
        cur.execute(
            """SELECT indexes_created,indexes_removed,operational_cost
            FROM charm_control.trial_actions WHERE trial_id=%s""",
            (trial_id,),
        )
        action_row = cur.fetchone()
        assert action_row is not None
        assert action_row["indexes_created"] == [candidate_id]
        assert action_row["indexes_removed"] == [candidate_id]
        assert action_row["operational_cost"]["index_size_bytes"] > 0
    artifact = settings.artifact_dir / str(artifact_row["relative_path"])
    assert artifact.stat().st_size == artifact_row["byte_size"]
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == artifact_row["sha256"]
    control_campaign(settings, campaign_id, "stop", "index recovery complete", "pytest")
