import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from threading import Event

import pytest

import charmdb.worker as worker
from charmdb.worker import TERMINAL_STATES, validate_transition


def test_durable_action_renews_lease_until_operation_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = worker.TrialLease(
        trial_id=uuid.uuid4(),
        campaign_id=uuid.uuid4(),
        state="VALIDATING_MEASUREMENT",
        workflow_kind="BASELINE_BENCHMARK",
        payload={},
        attempt_count=1,
        max_attempts=2,
        owner="unit-worker",
        token=uuid.uuid4(),
        expires_at=datetime.now(UTC) + timedelta(seconds=1),
        lease_seconds=1,
    )
    renewed_in_background = Event()
    heartbeat_calls = 0
    completed: list[dict[str, object]] = []

    monkeypatch.setattr(worker, "_record_action_start", lambda *_args: False)

    def fake_heartbeat(*_args: object, **_kwargs: object) -> datetime:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls >= 2:
            renewed_in_background.set()
        return datetime.now(UTC) + timedelta(seconds=1)

    monkeypatch.setattr(worker, "heartbeat", fake_heartbeat)
    monkeypatch.setattr(
        worker,
        "_record_action_complete",
        lambda _settings, _lease, _state, result: completed.append(result),
    )

    def operation() -> dict[str, object]:
        assert renewed_in_background.wait(timeout=2)
        return {"complete": True}

    result = worker._run_action(  # type: ignore[arg-type]
        object(), lease, "VALIDATING_MEASUREMENT", operation
    )

    assert result == {"complete": True}
    assert heartbeat_calls >= 2
    assert completed == [result]


def _lease(lease_seconds: int) -> worker.TrialLease:
    return worker.TrialLease(
        trial_id=uuid.uuid4(),
        campaign_id=uuid.uuid4(),
        state="RESTORING_CANDIDATE_DATASET",
        workflow_kind="V2_TUNED_BENCHMARK",
        payload={},
        attempt_count=1,
        max_attempts=1,
        owner="unit-worker",
        token=uuid.uuid4(),
        expires_at=datetime.now(UTC) + timedelta(seconds=lease_seconds),
        lease_seconds=lease_seconds,
    )


def _run_with_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    lease: worker.TrialLease,
    renewal: Callable[[int], datetime],
    operation: Callable[[], dict[str, object]],
) -> dict[str, object]:
    """Drive `_run_action` with a scripted heartbeat.

    `_run_action` heartbeats once synchronously as a fail-fast pre-check before
    starting the renewal thread, so `renewal` receives 1 for that call and 2+
    for background renewals.
    """
    calls = 0

    def dispatch(*_args: object, **_kwargs: object) -> datetime:
        nonlocal calls
        calls += 1
        return renewal(calls)

    monkeypatch.setattr(worker, "_record_action_start", lambda *_args: False)
    monkeypatch.setattr(worker, "heartbeat", dispatch)
    monkeypatch.setattr(worker, "_record_action_complete", lambda *_args: None)
    return worker._run_action(  # type: ignore[arg-type]
        object(), lease, lease.state, operation
    )


def _renewed(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def test_transient_heartbeat_errors_do_not_destroy_a_durable_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A control-database blip must not throw away a multi-minute observation.

    This is the D042 regression case: one transient error previously killed the
    renewal thread permanently and failed the entire trial.
    """
    lease = _lease(3)
    recovered = Event()

    def renewal(call: int) -> datetime:
        if call in {2, 3}:
            raise OSError("connection timeout expired")
        if call >= 4:
            recovered.set()
        return _renewed(3)

    def operation() -> dict[str, object]:
        assert recovered.wait(timeout=15)
        return {"restored": True}

    assert _run_with_heartbeat(monkeypatch, lease, renewal, operation) == {"restored": True}


def test_provably_lost_lease_still_fails_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuing under another owner's claim must never be tolerated."""
    lease = _lease(3)
    observed = Event()

    def renewal(call: int) -> datetime:
        if call == 1:
            return _renewed(3)
        observed.set()
        raise worker.TrialLeaseLost("trial lease was lost before heartbeat")

    def operation() -> dict[str, object]:
        assert observed.wait(timeout=15)
        return {"restored": True}

    with pytest.raises(RuntimeError) as error:
        _run_with_heartbeat(monkeypatch, lease, renewal, operation)

    assert "trial lease heartbeat failed" in str(error.value)
    assert "TrialLeaseLost" in str(error.value)
    assert isinstance(error.value.__cause__, worker.TrialLeaseLost)


def test_persistent_heartbeat_failure_beyond_the_lease_window_fails_with_its_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a full lease window passes with no renewal the lease is unsafe."""
    lease = _lease(1)
    attempted = Event()

    def renewal(call: int) -> datetime:
        if call == 1:
            return _renewed(1)
        attempted.set()
        raise OSError("control database is unreachable")

    def operation() -> dict[str, object]:
        assert attempted.wait(timeout=15)
        time.sleep(2.0)
        return {"restored": True}

    with pytest.raises(RuntimeError) as error:
        _run_with_heartbeat(monkeypatch, lease, renewal, operation)

    message = str(error.value)
    assert "transient error(s) within a 1-second lease window" in message
    assert "control database is unreachable" in message
    assert isinstance(error.value.__cause__, OSError)


def test_lost_lease_error_stays_a_runtime_error_for_existing_callers() -> None:
    assert issubclass(worker.TrialLeaseLost, RuntimeError)


def test_health_state_machine_rejects_skips_and_terminal_reentry() -> None:
    validate_transition("HEALTH_CHECK", "CREATED", "VERIFYING_DATABASE_HEALTH")
    validate_transition("HEALTH_CHECK", "VERIFYING_DATABASE_HEALTH", "PERSISTING_OBSERVATION")
    validate_transition("HEALTH_CHECK", "PERSISTING_OBSERVATION", "COMPLETED")
    with pytest.raises(ValueError, match="invalid HEALTH_CHECK transition"):
        validate_transition("HEALTH_CHECK", "CREATED", "COMPLETED")
    with pytest.raises(ValueError, match="terminal state"):
        validate_transition("HEALTH_CHECK", "COMPLETED", "CREATED")




def test_phase1_saturation_uses_benchmark_order_without_candidate_restore() -> None:
    validate_transition(
        worker.SATURATION_PHASE1_WORKFLOW,
        "CREATED",
        "CAPTURING_WORKLOAD_CONTEXT",
    )
    with pytest.raises(ValueError, match="V2_SATURATION_PHASE1"):
        validate_transition(
            worker.SATURATION_PHASE1_WORKFLOW,
            "CREATED",
            "RESTORING_CANDIDATE_DATASET",
        )




def test_v2_baseline_requires_restore_and_fingerprint_before_benchmark() -> None:
    chain = [
        "CREATED",
        "RESTORING_CANDIDATE_DATASET",
        "VERIFYING_CANDIDATE_BASELINE",
        "CAPTURING_WORKLOAD_CONTEXT",
        "VALIDATING_ACTIONS",
        "ESTIMATING_STATIC_RISK",
        "VERIFYING_DATABASE_HEALTH",
    ]
    for current, target in pairwise(chain):
        validate_transition("V2_BASELINE_BENCHMARK", current, target)
    with pytest.raises(ValueError, match="invalid V2_BASELINE_BENCHMARK transition"):
        validate_transition("V2_BASELINE_BENCHMARK", "CREATED", "CAPTURING_WORKLOAD_CONTEXT")


def test_v2_tuned_requires_restore_before_candidate_application() -> None:
    validate_transition("V2_TUNED_BENCHMARK", "CREATED", "RESTORING_CANDIDATE_DATASET")
    validate_transition(
        "V2_TUNED_BENCHMARK",
        "RESTORING_CANDIDATE_DATASET",
        "VERIFYING_CANDIDATE_BASELINE",
    )
    validate_transition(
        "V2_TUNED_BENCHMARK",
        "VERIFYING_CANDIDATE_BASELINE",
        "CAPTURING_WORKLOAD_CONTEXT",
    )
    validate_transition("V2_TUNED_BENCHMARK", "ESTIMATING_STATIC_RISK", "APPLYING_KNOBS")


def test_active_configuration_accepts_postgresql_real_canonicalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = worker.TrialLease(
        trial_id=uuid.uuid4(),
        campaign_id=uuid.uuid4(),
        state="VERIFYING_ACTIVE_CONFIGURATION",
        workflow_kind="V2_TUNED_BENCHMARK",
        payload={"expected_configuration": {"random_page_cost": "3.700422"}},
        attempt_count=1,
        max_attempts=3,
        owner="unit-worker",
        token=uuid.uuid4(),
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
        lease_seconds=30,
    )
    monkeypatch.setattr(
        worker,
        "discover_knobs",
        lambda *_args: [
            {
                "name": "random_page_cost",
                "setting": "3.70042",
                "vartype": "real",
                "pending_restart": False,
            }
        ],
    )

    result = worker._verify_baseline_configuration(object(), lease)  # type: ignore[arg-type]

    assert result["active_configuration"] == {"random_page_cost": "3.70042"}


def test_retry_reactivates_tuned_configuration_before_active_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = worker.TrialLease(
        trial_id=uuid.uuid4(),
        campaign_id=uuid.uuid4(),
        state="VERIFYING_ACTIVE_CONFIGURATION",
        workflow_kind="V2_TUNED_BENCHMARK",
        payload={},
        attempt_count=3,
        max_attempts=3,
        owner="unit-worker",
        token=uuid.uuid4(),
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
        lease_seconds=30,
    )
    actions: list[str] = []

    def fake_run_action(
        _settings: object,
        _lease: worker.TrialLease,
        state: str,
        operation: object,
    ) -> dict[str, object]:
        actions.append(state)
        if state == "RECOVERING_TUNED_CONFIGURATION_ATTEMPT_3":
            assert callable(operation)
            return {}
        raise RuntimeError("stop after recovery")

    monkeypatch.setattr(worker, "_run_action", fake_run_action)
    monkeypatch.setattr(worker, "_apply_tuned_configuration", lambda *_args: {})

    with pytest.raises(RuntimeError, match="stop after recovery"):
        worker._run_benchmark_once(  # type: ignore[arg-type]
            object(), lease, stale=False, lease_seconds=30, stop_after_state=None
        )

    assert actions[:2] == [
        "RECOVERING_TUNED_CONFIGURATION_ATTEMPT_3",
        "VERIFYING_ACTIVE_CONFIGURATION",
    ]


def test_v2_physical_archive_trial_requires_archive_identity() -> None:
    with pytest.raises(ValueError, match="physical_archive_id"):
        worker.create_baseline_benchmark_trial(
            object(),  # type: ignore[arg-type]
            uuid.uuid4(),
            uuid.uuid4(),
            "INFRASTRUCTURE",
            20260801,
            "missing-archive",
            restore_mechanism="physical-archive",
        )


def test_v2_tuned_trial_rejects_more_than_twelve_knobs_before_target_access() -> None:
    with pytest.raises(ValueError, match="one to twelve knobs"):
        worker.create_tuned_benchmark_trial(
            object(),  # type: ignore[arg-type]
            uuid.uuid4(),
            uuid.uuid4(),
            {f"knob_{index}": "1" for index in range(13)},
            20260831,
            "too-many-knobs",
        )


def test_all_required_failure_states_are_terminal() -> None:
    assert {
        "STATICALLY_INFEASIBLE",
        "PREDICTED_UNSAFE",
        "INVALID_CONFIGURATION",
        "APPLY_FAILED",
        "INDEX_BUILD_FAILED",
        "INDEX_DROP_FAILED",
        "RESTART_FAILED",
        "DATABASE_UNHEALTHY",
        "WORKLOAD_FAILED",
        "MEASUREMENT_INVALID",
        "SLO_VIOLATED",
        "RESOURCE_LIMIT_EXCEEDED",
        "DATASET_RESTORE_FAILED",
        "BASELINE_FINGERPRINT_FAILED",
        "LOW_FIDELITY_REJECTED",
        "EARLY_STOPPED",
        "CALIBRATION_INVALID",
        "TIMED_OUT",
        "ROLLED_BACK",
        "CANCELLED",
    } <= TERMINAL_STATES


def test_continuous_worker_drains_claimed_trial_after_shutdown_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = Event()
    trial_id = uuid.uuid4()
    calls = 0

    def fake_run_once(*_args: object, **_kwargs: object) -> worker.WorkerResult:
        nonlocal calls
        calls += 1
        shutdown.set()
        return worker.WorkerResult(True, trial_id, "COMPLETED", False)

    monkeypatch.setattr(worker, "run_once", fake_run_once)
    events: list[dict[str, object]] = []
    result = worker.run_worker_service(
        object(),  # type: ignore[arg-type]
        owner="continuous-test",
        shutdown_event=shutdown,
        poll_seconds=0.01,
        event_callback=events.append,
    )
    assert calls == 1
    assert result.processed_trials == 1
    assert result.state_counts == {"COMPLETED": 1}
    assert result.shutdown_requested
    assert result.exit_reason == "shutdown_requested"
    assert [event["event"] for event in events] == [
        "WORKER_SERVICE_STARTED",
        "WORKER_TRIAL_FINISHED",
        "WORKER_SERVICE_STOPPED",
    ]


def test_continuous_worker_has_bounded_idle_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker,
        "run_once",
        lambda *_args, **_kwargs: worker.WorkerResult(False, None, None, False),
    )
    result = worker.run_worker_service(
        object(),  # type: ignore[arg-type]
        owner="idle-test",
        poll_seconds=0.001,
        idle_exit_seconds=0.002,
    )
    assert result.processed_trials == 0
    assert result.idle_polls >= 1
    assert result.exit_reason == "idle_timeout"
