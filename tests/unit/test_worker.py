import uuid
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from threading import Event

import pytest

import charmdb.search_execution as search_execution
import charmdb.worker as worker
from charmdb.api import app
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


def test_terminal_worker_hook_attempts_search_budget_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial_id = uuid.uuid4()
    calls: list[tuple[object, uuid.UUID, str]] = []
    monkeypatch.setattr(
        search_execution,
        "reconcile_terminal_search_trial",
        lambda settings, trial, actor: calls.append((settings, trial, actor)),
    )
    settings = object()

    worker._reconcile_experiment_trial_best_effort(settings, trial_id)  # type: ignore[arg-type]

    assert calls == [(settings, trial_id, "durable-worker")]


def test_health_state_machine_rejects_skips_and_terminal_reentry() -> None:
    validate_transition("HEALTH_CHECK", "CREATED", "VERIFYING_DATABASE_HEALTH")
    validate_transition("HEALTH_CHECK", "VERIFYING_DATABASE_HEALTH", "PERSISTING_OBSERVATION")
    validate_transition("HEALTH_CHECK", "PERSISTING_OBSERVATION", "COMPLETED")
    with pytest.raises(ValueError, match="invalid HEALTH_CHECK transition"):
        validate_transition("HEALTH_CHECK", "CREATED", "COMPLETED")
    with pytest.raises(ValueError, match="terminal state"):
        validate_transition("HEALTH_CHECK", "COMPLETED", "CREATED")


def test_baseline_benchmark_state_machine_requires_measurement_order() -> None:
    chain = [
        "CREATED",
        "CAPTURING_WORKLOAD_CONTEXT",
        "VALIDATING_ACTIONS",
        "ESTIMATING_STATIC_RISK",
        "VERIFYING_DATABASE_HEALTH",
        "VERIFYING_ACTIVE_CONFIGURATION",
        "WARMING_UP",
        "RESETTING_OR_SNAPSHOTTING_COUNTERS",
        "RUNNING_FULL_EVALUATION",
        "COLLECTING_METRICS",
        "VALIDATING_MEASUREMENT",
        "CALCULATING_OBJECTIVES",
        "CALCULATING_CONSTRAINTS",
        "PERSISTING_OBSERVATION",
        "COMPLETED",
    ]
    for current, target in pairwise(chain):
        validate_transition("BASELINE_BENCHMARK", current, target)
    with pytest.raises(ValueError, match="invalid BASELINE_BENCHMARK transition"):
        validate_transition("BASELINE_BENCHMARK", "WARMING_UP", "COMPLETED")


def test_tuned_benchmark_adds_apply_activation_and_rollback_states() -> None:
    validate_transition("TUNED_BENCHMARK", "ESTIMATING_STATIC_RISK", "APPLYING_KNOBS")
    validate_transition("TUNED_BENCHMARK", "APPLYING_KNOBS", "RELOADING_OR_RESTARTING")
    validate_transition("TUNED_BENCHMARK", "RELOADING_OR_RESTARTING", "VERIFYING_DATABASE_HEALTH")
    validate_transition("TUNED_BENCHMARK", "PERSISTING_OBSERVATION", "SELECTING_NEXT_ACTION")
    validate_transition("TUNED_BENCHMARK", "SELECTING_NEXT_ACTION", "COMPLETED")


def test_index_lifecycle_requires_build_verify_and_drop_order() -> None:
    chain = [
        "CREATED",
        "VALIDATING_ACTIONS",
        "BUILDING_INDEXES",
        "VERIFYING_ACTIVE_CONFIGURATION",
        "SELECTING_NEXT_ACTION",
        "COMPLETED",
    ]
    for current, target in pairwise(chain):
        validate_transition("INDEX_LIFECYCLE", current, target)
    with pytest.raises(ValueError, match="invalid INDEX_LIFECYCLE transition"):
        validate_transition("INDEX_LIFECYCLE", "BUILDING_INDEXES", "COMPLETED")


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
        "LOW_FIDELITY_REJECTED",
        "EARLY_STOPPED",
        "CALIBRATION_INVALID",
        "TIMED_OUT",
        "ROLLED_BACK",
        "CANCELLED",
    } <= TERMINAL_STATES


def test_openapi_exposes_typed_campaign_and_recovery_controls() -> None:
    schema = app.openapi()
    paths = schema["paths"]
    assert "/campaigns" in paths
    assert "/metrics" in paths
    assert "/campaigns/{campaign_id}/pause" in paths
    assert "/campaigns/{campaign_id}/resume" in paths
    assert "/campaigns/{campaign_id}/stop" in paths
    assert "/campaigns/{campaign_id}/emergency-stop" in paths
    assert "/campaigns/{campaign_id}/trials/health" in paths
    assert "/campaigns/{campaign_id}/trials/baseline-benchmark" in paths
    assert "/campaigns/{campaign_id}/trials/tuned-benchmark" in paths
    assert "/campaigns/{campaign_id}/trials/index-lifecycle" in paths
    assert "/trials/{trial_id}" in paths
    assert "/reports" in paths
    assert "CampaignCreateRequest" in schema["components"]["schemas"]
    assert "BaselineBenchmarkTrialRequest" in schema["components"]["schemas"]
    assert "TunedBenchmarkTrialRequest" in schema["components"]["schemas"]
    assert "IndexLifecycleTrialRequest" in schema["components"]["schemas"]


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
