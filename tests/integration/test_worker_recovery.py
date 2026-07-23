import os
import uuid
from pathlib import Path
from threading import Event

import pytest

from charmdb.config import get_settings
from charmdb.db import apply_migrations, connect
from charmdb.worker import (
    campaign_status,
    control_campaign,
    create_campaign,
    create_health_trial,
    run_once,
    run_worker_service,
    trial_history,
)

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not (os.getenv("CHARMDB_TARGET_DSN") or Path(".env").exists()),
    reason="integration environment not configured",
)
def test_continuous_worker_drains_then_resumes_campaign() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    campaign_id = create_campaign(
        settings,
        f"continuous-worker-{uuid.uuid4().hex[:8]}",
        "RELIABILITY_SERVICE",
        {},
        {"target_must_be_writable": True},
        actor="pytest",
    )
    control_campaign(settings, campaign_id, "resume", "start continuous worker test", "pytest")
    first_trial = create_health_trial(settings, campaign_id, 20260725, "continuous-health-1")
    second_trial = create_health_trial(settings, campaign_id, 20260726, "continuous-health-2")
    shutdown = Event()

    def request_stop_after_finished(event: dict[str, object]) -> None:
        if event["event"] == "WORKER_TRIAL_FINISHED":
            shutdown.set()

    first_service = run_worker_service(
        settings,
        owner="continuous-worker-first",
        campaign_id=campaign_id,
        shutdown_event=shutdown,
        poll_seconds=0.01,
        event_callback=request_stop_after_finished,
    )
    assert first_service.processed_trials == 1
    assert first_service.shutdown_requested
    assert first_service.exit_reason == "shutdown_requested"
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT trial_id,state FROM charm_control.trials WHERE campaign_id=%s",
            (campaign_id,),
        )
        states = {row["trial_id"]: str(row["state"]) for row in cur.fetchall()}
    assert sorted(states.values()) == ["COMPLETED", "CREATED"]

    second_service = run_worker_service(
        settings,
        owner="continuous-worker-second",
        campaign_id=campaign_id,
        poll_seconds=0.01,
        max_trials=1,
    )
    assert second_service.processed_trials == 1
    assert second_service.exit_reason == "max_trials_reached"
    assert {
        trial_history(settings, first_trial)["state"],
        trial_history(settings, second_trial)["state"],
    } == {"COMPLETED"}
    control_campaign(settings, campaign_id, "stop", "continuous worker test complete", "pytest")


@pytest.mark.skipif(
    not (os.getenv("CHARMDB_TARGET_DSN") or Path(".env").exists()),
    reason="integration environment not configured",
)
def test_stale_lease_recovery_is_idempotent_and_completes_real_health_workflow() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    campaign_id = create_campaign(
        settings,
        f"worker-recovery-{uuid.uuid4().hex[:8]}",
        "MONITOR",
        {},
        {"target_must_be_writable": True},
        failure_limit=2,
        actor="pytest",
    )
    control_campaign(settings, campaign_id, "resume", "start recovery test", "pytest")
    idempotency_key = f"health-{uuid.uuid4()}"
    trial_id = create_health_trial(settings, campaign_id, 20260713, idempotency_key)
    duplicate_id = create_health_trial(settings, campaign_id, 20260713, idempotency_key)
    assert duplicate_id == trial_id

    interrupted = run_once(
        settings,
        owner="worker-before-crash",
        lease_seconds=30,
        stop_after_state="VERIFYING_DATABASE_HEALTH",
        campaign_id=campaign_id,
    )
    assert interrupted.trial_id == trial_id
    assert interrupted.state == "VERIFYING_DATABASE_HEALTH"

    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.trials
               SET lease_expires_at=clock_timestamp() - interval '1 second'
               WHERE trial_id=%s""",
            (trial_id,),
        )
        conn.commit()

    recovered = run_once(settings, owner="worker-after-crash", campaign_id=campaign_id)
    assert recovered.trial_id == trial_id
    assert recovered.recovered_stale_lease
    assert recovered.state == "COMPLETED"

    history = trial_history(settings, trial_id)
    assert history["state"] == "COMPLETED"
    assert history["attempt_count"] == 2
    assert history["workflow_result"]["database"] == settings.target_db
    assert history["workflow_result"]["writable"] is True
    assert [row["to_state"] for row in history["transitions"]] == [
        "CREATED",
        "VERIFYING_DATABASE_HEALTH",
        "PERSISTING_OBSERVATION",
        "COMPLETED",
    ]
    assert {row["state"] for row in history["actions"]} == {
        "VERIFYING_DATABASE_HEALTH",
        "PERSISTING_OBSERVATION",
    }
    assert all(row["status"] == "COMPLETED" for row in history["actions"])
    control_campaign(settings, campaign_id, "stop", "recovery test complete", "pytest")


@pytest.mark.skipif(
    not (os.getenv("CHARMDB_TARGET_DSN") or Path(".env").exists()),
    reason="integration environment not configured",
)
def test_pause_resume_and_emergency_stop_cancel_unstarted_work() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    campaign_id = create_campaign(
        settings,
        f"worker-control-{uuid.uuid4().hex[:8]}",
        "MONITOR",
        {},
        {},
        actor="pytest",
    )
    control_campaign(settings, campaign_id, "resume", "start", "pytest")
    control_campaign(settings, campaign_id, "pause", "operator pause", "pytest")
    assert campaign_status(settings, campaign_id)["status"] == "PAUSED"
    control_campaign(settings, campaign_id, "resume", "operator resume", "pytest")
    trial_id = create_health_trial(
        settings, campaign_id, 20260713, f"cancel-{uuid.uuid4()}", max_attempts=1
    )
    result = control_campaign(
        settings, campaign_id, "emergency-stop", "controlled negative case", "pytest"
    )
    assert result["status"] == "STOPPED"
    assert result["cancelled_trials"] == 1
    history = trial_history(settings, trial_id)
    assert history["state"] == "CANCELLED"
    assert history["failure_type"] == "CANCELLED"
    status = campaign_status(settings, campaign_id)
    assert status["emergency_stop"] is True
    assert status["trial_counts"] == {"CANCELLED": 1}
