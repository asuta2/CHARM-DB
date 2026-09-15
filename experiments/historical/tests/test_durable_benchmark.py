import hashlib
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import charmdb.controller as controller
import charmdb.worker as worker
from charmdb.config import Settings, get_settings
from charmdb.db import apply_migrations, connect
from charmdb.worker import (
    control_campaign,
    create_baseline_benchmark_trial,
    create_campaign,
    create_health_trial,
    create_tuned_benchmark_trial,
    run_once,
    trial_history,
)

pytestmark = pytest.mark.integration


def _configured() -> bool:
    return bool(os.getenv("CHARMDB_TARGET_DSN") or Path(".env").exists())


def _stop_test_campaign(settings: Settings, campaign_id: uuid.UUID, reason: str) -> None:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM charm_control.campaigns WHERE campaign_id=%s", (campaign_id,)
        )
        row = cur.fetchone()
    if row is not None and row["status"] in {"CREATED", "RUNNING", "PAUSED"}:
        control_campaign(settings, campaign_id, "stop", reason, "pytest")


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_durable_default_benchmark_persists_complete_measurement(
    request: pytest.FixtureRequest,
) -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    campaign_id = create_campaign(
        settings,
        f"durable-benchmark-{uuid.uuid4().hex[:8]}",
        "RESEARCH_BASELINE",
        {"maximize": "throughput_tps"},
        {"p99_ms_max": 20.0, "failures_max": 0},
        actor="pytest",
    )
    request.addfinalizer(
        lambda: _stop_test_campaign(settings, campaign_id, "benchmark test finalizer")
    )
    control_campaign(settings, campaign_id, "resume", "start durable benchmark", "pytest")
    trial_id = create_baseline_benchmark_trial(
        settings,
        campaign_id,
        20260718,
        f"durable-default-{uuid.uuid4()}",
        warmup_seconds=1,
        duration_seconds=3,
        concurrency=2,
        max_attempts=2,
    )
    result = run_once(
        settings,
        owner="durable-benchmark-worker",
        lease_seconds=120,
        campaign_id=campaign_id,
    )
    assert result.trial_id == trial_id

    history = trial_history(settings, trial_id)
    feasible = (
        history["workflow_result"]["failures"] == 0 and history["workflow_result"]["p99_ms"] <= 20.0
    )
    expected_state = "COMPLETED" if feasible else "SLO_VIOLATED"
    assert result.state == expected_state
    assert history["state"] == expected_state
    assert history["workflow_result"]["transactions"] > 0
    assert history["workflow_result"]["latency_samples"] > 0
    assert history["workflow_result"]["throughput_tps"] > 0
    assert history["workflow_result"]["p99_ms"] > 0
    assert len(history["actions"]) == 13
    assert all(action["status"] == "COMPLETED" for action in history["actions"])

    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT relative_path,sha256,byte_size FROM charm_control.artifacts
            WHERE trial_id=%s AND kind='durable-trial-json'""",
            (trial_id,),
        )
        artifact = cur.fetchone()
        assert artifact is not None
        path = settings.artifact_dir / artifact["relative_path"]
        assert path.stat().st_size == artifact["byte_size"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
        cur.execute(
            """SELECT phase,count(*) AS count FROM charm_control.metric_snapshots
            WHERE trial_id=%s GROUP BY phase ORDER BY phase""",
            (trial_id,),
        )
        assert cur.fetchall() == [
            {"phase": "after", "count": 1},
            {"phase": "before", "count": 1},
        ]
    control_campaign(settings, campaign_id, "stop", "benchmark test complete", "pytest")


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_long_measurement_renews_short_lease(request: pytest.FixtureRequest) -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    campaign_id = create_campaign(
        settings,
        f"heartbeat-benchmark-{uuid.uuid4().hex[:8]}",
        "RESEARCH_BASELINE",
        {"maximize": "throughput_tps"},
        {"failures_max": 0},
        actor="pytest",
    )
    request.addfinalizer(
        lambda: _stop_test_campaign(settings, campaign_id, "heartbeat benchmark finalizer")
    )
    control_campaign(settings, campaign_id, "resume", "start heartbeat benchmark", "pytest")
    trial_id = create_baseline_benchmark_trial(
        settings,
        campaign_id,
        20260722,
        f"heartbeat-benchmark-{uuid.uuid4()}",
        warmup_seconds=0,
        duration_seconds=12,
        concurrency=1,
        max_attempts=2,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            run_once,
            settings,
            "heartbeat-worker",
            5,
            campaign_id=campaign_id,
        )
        deadline = time.monotonic() + 15
        initial_heartbeat = None
        while time.monotonic() < deadline:
            with connect(settings.control_dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT state,heartbeat_at FROM charm_control.trials WHERE trial_id=%s""",
                    (trial_id,),
                )
                row = cur.fetchone()
            if row and row["state"] == "RUNNING_FULL_EVALUATION":
                initial_heartbeat = row["heartbeat_at"]
                break
            time.sleep(0.1)
        assert initial_heartbeat is not None
        time.sleep(6.0)
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT lease_owner,heartbeat_at,
                          lease_expires_at > clock_timestamp() AS lease_current
                FROM charm_control.trials WHERE trial_id=%s""",
                (trial_id,),
            )
            active = cur.fetchone()
        assert active is not None
        assert active["lease_owner"] == "heartbeat-worker"
        assert active["heartbeat_at"] > initial_heartbeat
        assert active["lease_current"] is True
        result = future.result(timeout=30)
    history = trial_history(settings, trial_id)
    feasible = (
        history["workflow_result"]["failures"] == 0 and history["workflow_result"]["p99_ms"] <= 20.0
    )
    assert result.state == ("COMPLETED" if feasible else "SLO_VIOLATED")
    assert history["attempt_count"] == 1
    control_campaign(settings, campaign_id, "stop", "heartbeat benchmark complete", "pytest")


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_real_worker_process_kill_is_recovered_after_lease_expiry() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    campaign_id = create_campaign(
        settings,
        f"process-kill-{uuid.uuid4().hex[:8]}",
        "MONITOR",
        {},
        {},
        actor="pytest",
    )
    control_campaign(settings, campaign_id, "resume", "start process kill test", "pytest")
    trial_id = create_health_trial(
        settings, campaign_id, 20260718, f"process-kill-{uuid.uuid4()}", max_attempts=3
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "charmdb.cli",
            "worker-run-once",
            "--owner",
            "worker-that-will-be-killed",
            "--lease-seconds",
            "5",
            "--stop-after-state",
            "VERIFYING_DATABASE_HEALTH",
            "--hold-seconds",
            "30",
            "--campaign-id",
            str(campaign_id),
        ],
        cwd=Path.cwd(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 15
    observed = False
    while time.monotonic() < deadline:
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT state,lease_owner FROM charm_control.trials WHERE trial_id=%s", (trial_id,)
            )
            row = cur.fetchone()
        if row and row["state"] == "VERIFYING_DATABASE_HEALTH" and row["lease_owner"]:
            observed = True
            break
        time.sleep(0.1)
    assert observed
    process.kill()
    process.wait(timeout=10)
    time.sleep(5.2)

    recovered = run_once(
        settings, owner="replacement-worker", lease_seconds=30, campaign_id=campaign_id
    )
    assert recovered.trial_id == trial_id
    assert recovered.recovered_stale_lease
    assert recovered.state == "COMPLETED"
    history = trial_history(settings, trial_id)
    assert history["attempt_count"] == 2
    assert history["state"] == "COMPLETED"
    control_campaign(settings, campaign_id, "stop", "process kill test complete", "pytest")


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_completed_measurement_marker_prevents_rerunning_ambiguous_workload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    campaign_id = create_campaign(
        settings,
        f"marker-recovery-{uuid.uuid4().hex[:8]}",
        "RESEARCH_BASELINE",
        {"maximize": "throughput_tps"},
        {"failures_max": 0},
        actor="pytest",
    )
    control_campaign(settings, campaign_id, "resume", "start marker recovery", "pytest")
    trial_id = create_baseline_benchmark_trial(
        settings,
        campaign_id,
        20260719,
        f"marker-recovery-{uuid.uuid4()}",
        warmup_seconds=0,
        duration_seconds=2,
        concurrency=1,
        p99_slo_ms=1000.0,
        max_attempts=2,
    )
    original_complete = worker._record_action_complete
    interrupted = False

    def interrupt_after_measurement(
        _settings_arg: object,
        lease: worker.TrialLease,
        state: str,
        result: dict[str, object],
    ) -> None:
        nonlocal interrupted
        if state == "RUNNING_FULL_EVALUATION" and not interrupted:
            interrupted = True
            raise RuntimeError("controlled crash after atomic measurement marker")
        original_complete(settings, lease, state, result)

    monkeypatch.setattr(worker, "_record_action_complete", interrupt_after_measurement)
    with pytest.raises(RuntimeError, match="controlled crash"):
        run_once(
            settings,
            owner="marker-worker-before",
            lease_seconds=120,
            campaign_id=campaign_id,
        )
    markers = list(
        (settings.artifact_dir / "raw" / str(campaign_id) / str(trial_id)).glob(
            "measurement-attempt-*.json"
        )
    )
    assert len(markers) == 1
    time.sleep(1.1)

    recovered = run_once(
        settings,
        owner="marker-worker-after",
        lease_seconds=120,
        campaign_id=campaign_id,
    )
    assert recovered.trial_id == trial_id
    assert recovered.state == "COMPLETED"
    history = trial_history(settings, trial_id)
    measurement = next(
        action for action in history["actions"] if action["state"] == "RUNNING_FULL_EVALUATION"
    )
    assert measurement["attempt"] == 2
    assert measurement["result"]["recovered_marker"] is True
    assert (
        len(
            list(
                (settings.artifact_dir / "raw" / str(campaign_id) / str(trial_id)).glob(
                    "measurement-attempt-*.json"
                )
            )
        )
        == 1
    )
    control_campaign(settings, campaign_id, "stop", "marker recovery complete", "pytest")


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_orphan_pgbench_session_is_terminated_before_retry() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    campaign_id = create_campaign(
        settings,
        f"orphan-workload-{uuid.uuid4().hex[:8]}",
        "RELIABILITY_NEGATIVE",
        {"validate": "orphan_cleanup"},
        {"target_session_isolation": True},
        actor="pytest",
    )
    control_campaign(settings, campaign_id, "resume", "start orphan cleanup", "pytest")
    trial_id = create_baseline_benchmark_trial(
        settings,
        campaign_id,
        20260728,
        f"orphan-workload-{uuid.uuid4()}",
        warmup_seconds=0,
        duration_seconds=2,
        concurrency=1,
        p99_slo_ms=1000.0,
        max_attempts=2,
    )
    application_name = f"charmdb:{trial_id}:measurement"
    environment = os.environ.copy()
    environment["PGPASSWORD"] = settings.target_password
    environment["PGAPPNAME"] = application_name
    orphan = subprocess.Popen(
        [
            "pgbench",
            "-h",
            settings.target_host,
            "-p",
            str(settings.target_port),
            "-U",
            settings.target_user,
            "-d",
            settings.target_db,
            "-c",
            "1",
            "-T",
            "30",
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    observed = False
    while time.monotonic() < deadline:
        with connect(settings.target_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS count FROM pg_stat_activity WHERE application_name=%s",
                (application_name,),
            )
            observed = int(cur.fetchone()["count"]) > 0  # type: ignore[index]
        if observed:
            break
        time.sleep(0.1)
    assert observed
    try:
        result = run_once(
            settings,
            owner="orphan-cleanup-worker",
            lease_seconds=30,
            campaign_id=campaign_id,
        )
        assert result.state == "COMPLETED"
        orphan.wait(timeout=10)
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait(timeout=10)
    history = trial_history(settings, trial_id)
    measurement = next(
        action for action in history["actions"] if action["state"] == "RUNNING_FULL_EVALUATION"
    )
    cleanup = measurement["result"]["orphan_cleanup"]
    assert cleanup["terminated_sessions"] >= 1
    assert cleanup["remaining_sessions"] == 0
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS count FROM pg_stat_activity WHERE application_name=%s",
            (application_name,),
        )
        assert cur.fetchone() == {"count": 0}
    control_campaign(settings, campaign_id, "stop", "orphan cleanup complete", "pytest")


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_tuned_reload_trial_measures_and_restores_default(
    request: pytest.FixtureRequest,
) -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    campaign_id = create_campaign(
        settings,
        f"durable-tuned-{uuid.uuid4().hex[:8]}",
        "RESEARCH_TUNED",
        {"maximize": "throughput_tps"},
        {"p99_ms_max": 20.0},
        actor="pytest",
    )
    request.addfinalizer(
        lambda: _stop_test_campaign(settings, campaign_id, "tuned trial finalizer")
    )
    control_campaign(settings, campaign_id, "resume", "start tuned trial", "pytest")
    trial_id = create_tuned_benchmark_trial(
        settings,
        campaign_id,
        {"random_page_cost": "3.9"},
        20260720,
        f"durable-tuned-{uuid.uuid4()}",
        warmup_seconds=0,
        duration_seconds=2,
        concurrency=1,
        max_attempts=2,
    )
    result = run_once(settings, owner="tuned-worker", lease_seconds=120, campaign_id=campaign_id)
    assert result.trial_id == trial_id
    history = trial_history(settings, trial_id)
    feasible = (
        history["workflow_result"]["failures"] == 0 and history["workflow_result"]["p99_ms"] <= 20.0
    )
    assert result.state == ("COMPLETED" if feasible else "ROLLED_BACK")
    states = {action["state"] for action in history["actions"]}
    assert {"APPLYING_KNOBS", "RELOADING_OR_RESTARTING", "SELECTING_NEXT_ACTION"} <= states
    assert history["workflow_result"]["transactions"] > 0
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT setting,pending_restart FROM pg_settings WHERE name='random_page_cost'")
        active = cur.fetchone()
        assert active == {"setting": "4", "pending_restart": False}
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT a.status,count(r.rollback_id) AS rollbacks
            FROM charm_control.configuration_applications a
            LEFT JOIN charm_control.rollbacks r USING(application_id)
            WHERE a.application_id=%s GROUP BY a.application_id""",
            (worker._application_ids(trial_id)[0],),
        )
        assert cur.fetchone() == {"status": "ROLLED_BACK", "rollbacks": 1}
    control_campaign(settings, campaign_id, "stop", "tuned trial complete", "pytest")


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_ambiguous_apply_commit_reconciles_without_duplicate_alter_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    campaign_id = create_campaign(
        settings,
        f"apply-recovery-{uuid.uuid4().hex[:8]}",
        "RESEARCH_TUNED",
        {"maximize": "throughput_tps"},
        {"p99_ms_max": 20.0},
        actor="pytest",
    )
    control_campaign(settings, campaign_id, "resume", "start apply recovery", "pytest")
    trial_id = create_tuned_benchmark_trial(
        settings,
        campaign_id,
        {"random_page_cost": "3.8"},
        20260721,
        f"apply-recovery-{uuid.uuid4()}",
        warmup_seconds=0,
        duration_seconds=2,
        concurrency=1,
        max_attempts=2,
    )
    original_set_system = controller._set_system
    original_complete = worker._record_action_complete
    set_calls = 0
    interrupted = False

    def count_set_system(settings_arg: object, values: dict[str, str]) -> None:
        nonlocal set_calls
        set_calls += 1
        original_set_system(settings, values)

    def terminate_after_apply_commit(
        _settings_arg: object,
        lease: worker.TrialLease,
        state: str,
        result: dict[str, object],
    ) -> None:
        nonlocal interrupted
        if state == "APPLYING_KNOBS" and not interrupted:
            interrupted = True
            raise SystemExit("controlled process death after controller commit")
        original_complete(settings, lease, state, result)

    monkeypatch.setattr(controller, "_set_system", count_set_system)
    monkeypatch.setattr(worker, "_record_action_complete", terminate_after_apply_commit)
    with pytest.raises(SystemExit, match="controlled process death"):
        run_once(
            settings,
            owner="apply-worker-before",
            lease_seconds=30,
            campaign_id=campaign_id,
        )
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT setting FROM pg_settings WHERE name='random_page_cost'")
        assert cur.fetchone() == {"setting": "3.8"}
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.trials
            SET lease_expires_at=clock_timestamp() - interval '1 second'
            WHERE trial_id=%s""",
            (trial_id,),
        )
        conn.commit()

    recovered = run_once(
        settings,
        owner="apply-worker-after",
        lease_seconds=120,
        campaign_id=campaign_id,
    )
    assert recovered.recovered_stale_lease
    assert recovered.state == "COMPLETED"
    assert set_calls == 2  # one apply and one final rollback; recovery did not reapply
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT setting FROM pg_settings WHERE name='random_page_cost'")
        assert cur.fetchone() == {"setting": "4"}
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) AS count
            FROM charm_control.configuration_applications WHERE application_id=%s""",
            (worker._application_ids(trial_id)[0],),
        )
        assert cur.fetchone() == {"count": 1}
    control_campaign(settings, campaign_id, "stop", "apply recovery complete", "pytest")


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_restart_class_apply_commit_recovers_without_duplicate_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    campaign_id = create_campaign(
        settings,
        f"restart-recovery-{uuid.uuid4().hex[:8]}",
        "RESEARCH_TUNED",
        {"maximize": "throughput_tps"},
        {"p99_ms_max": 1000.0},
        actor="pytest",
    )
    control_campaign(settings, campaign_id, "resume", "start restart recovery", "pytest")
    trial_id = create_tuned_benchmark_trial(
        settings,
        campaign_id,
        {"shared_buffers": "20480"},
        20260722,
        f"restart-recovery-{uuid.uuid4()}",
        warmup_seconds=0,
        duration_seconds=2,
        concurrency=1,
        p99_slo_ms=1000.0,
        max_attempts=2,
    )
    original_set_system = controller._set_system
    original_restart = controller._restart_target
    original_complete = worker._record_action_complete
    set_calls = 0
    restart_calls = 0
    interrupted = False

    def count_set_system(settings_arg: object, values: dict[str, str]) -> None:
        nonlocal set_calls
        set_calls += 1
        original_set_system(settings, values)

    def count_restart() -> None:
        nonlocal restart_calls
        restart_calls += 1
        original_restart()

    def terminate_after_restart_commit(
        _settings_arg: object,
        lease: worker.TrialLease,
        state: str,
        result: dict[str, object],
    ) -> None:
        nonlocal interrupted
        if state == "APPLYING_KNOBS" and not interrupted:
            interrupted = True
            raise SystemExit("controlled process death after restart application commit")
        original_complete(settings, lease, state, result)

    monkeypatch.setattr(controller, "_set_system", count_set_system)
    monkeypatch.setattr(controller, "_restart_target", count_restart)
    monkeypatch.setattr(worker, "_record_action_complete", terminate_after_restart_commit)
    with pytest.raises(SystemExit, match="controlled process death"):
        run_once(
            settings,
            owner="restart-worker-before",
            lease_seconds=30,
            campaign_id=campaign_id,
        )
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT setting,pending_restart FROM pg_settings WHERE name='shared_buffers'")
        assert cur.fetchone() == {"setting": "20480", "pending_restart": False}
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.trials
            SET lease_expires_at=clock_timestamp() - interval '1 second'
            WHERE trial_id=%s""",
            (trial_id,),
        )
        conn.commit()

    recovered = run_once(
        settings,
        owner="restart-worker-after",
        lease_seconds=120,
        campaign_id=campaign_id,
    )
    assert recovered.recovered_stale_lease
    assert recovered.state == "COMPLETED"
    assert set_calls == 2  # initial apply plus rollback; recovery did not reapply
    assert restart_calls == 2  # initial activation plus rollback; recovery did not restart
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT setting,pending_restart FROM pg_settings WHERE name='shared_buffers'")
        assert cur.fetchone() == {"setting": "16384", "pending_restart": False}
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT a.status,a.requires_restart,count(r.rollback_id) AS rollbacks
            FROM charm_control.configuration_applications a
            LEFT JOIN charm_control.rollbacks r USING(application_id)
            WHERE a.application_id=%s
            GROUP BY a.application_id""",
            (worker._application_ids(trial_id)[0],),
        )
        assert cur.fetchone() == {
            "status": "ROLLED_BACK",
            "requires_restart": True,
            "rollbacks": 1,
        }
    control_campaign(settings, campaign_id, "stop", "restart recovery complete", "pytest")


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_restart_class_pre_activation_death_retries_and_restores_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    campaign_id = create_campaign(
        settings,
        f"restart-pre-activation-{uuid.uuid4().hex[:8]}",
        "RESEARCH_TUNED",
        {"maximize": "throughput_tps"},
        {"p99_ms_max": 1000.0},
        actor="pytest",
    )
    control_campaign(settings, campaign_id, "resume", "start pre-activation recovery", "pytest")
    trial_id = create_tuned_benchmark_trial(
        settings,
        campaign_id,
        {"shared_buffers": "20480"},
        20260723,
        f"restart-pre-activation-{uuid.uuid4()}",
        warmup_seconds=0,
        duration_seconds=2,
        concurrency=1,
        p99_slo_ms=1000.0,
        max_attempts=2,
    )
    original_restart = controller._restart_target
    restart_calls = 0

    def die_before_first_restart() -> None:
        nonlocal restart_calls
        restart_calls += 1
        if restart_calls == 1:
            raise SystemExit("controlled process death before restart activation")
        original_restart()

    monkeypatch.setattr(controller, "_restart_target", die_before_first_restart)
    with pytest.raises(SystemExit, match="before restart activation"):
        run_once(
            settings,
            owner="pre-activation-worker-before",
            lease_seconds=30,
            campaign_id=campaign_id,
        )
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT setting,pending_restart FROM pg_settings WHERE name='shared_buffers'")
        assert cur.fetchone() == {"setting": "16384", "pending_restart": False}
        cur.execute(
            """SELECT setting FROM pg_file_settings
            WHERE name='shared_buffers' AND sourcefile LIKE '%%postgresql.auto.conf'
            ORDER BY seqno DESC LIMIT 1"""
        )
        assert cur.fetchone() == {"setting": "20480"}
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.trials
            SET lease_expires_at=clock_timestamp() - interval '1 second'
            WHERE trial_id=%s""",
            (trial_id,),
        )
        conn.commit()

    recovered = run_once(
        settings,
        owner="pre-activation-worker-after",
        lease_seconds=120,
        campaign_id=campaign_id,
    )
    assert recovered.recovered_stale_lease
    assert recovered.state == "COMPLETED"
    assert restart_calls == 3  # failed activation, recovered activation, final rollback
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT setting,pending_restart FROM pg_settings WHERE name='shared_buffers'")
        assert cur.fetchone() == {"setting": "16384", "pending_restart": False}
    control_campaign(
        settings,
        campaign_id,
        "stop",
        "pre-activation restart recovery complete",
        "pytest",
    )
