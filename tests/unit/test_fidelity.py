from charmdb.fidelity import decide_early_stop, decide_promotion, early_stopping_gate
from charmdb.workload import WorkloadResult


def _result(tps: float, p99: float = 10, failures: int = 0) -> WorkloadResult:
    return WorkloadResult(100, failures, tps, 5, 8, p99, 5, 100)


def test_promotion_requires_feasibility_and_performance_floor() -> None:
    assert decide_promotion(_result(450), 500).promoted
    assert not decide_promotion(_result(350), 500).promoted
    assert not decide_promotion(_result(450, p99=25), 500).promoted
    assert not decide_promotion(_result(450, failures=1), 500).promoted


def test_early_stop_has_protected_window_and_conservative_boundaries() -> None:
    assert not decide_early_stop(100, 50, 500, elapsed_seconds=4).stopped
    assert decide_early_stop(300, 10, 500, elapsed_seconds=5).stopped
    assert decide_early_stop(500, 31, 500, elapsed_seconds=5).stopped
    assert not decide_early_stop(480, 12, 500, elapsed_seconds=5).stopped


def test_fidelity_pilot_budget_requires_paired_evidence_floor() -> None:
    assert not early_stopping_gate(4, 0.8, 0)
    assert early_stopping_gate(12, 0.8, 0)
    assert not early_stopping_gate(12, 0.4, 0)
    assert not early_stopping_gate(12, 0.8, 1)
