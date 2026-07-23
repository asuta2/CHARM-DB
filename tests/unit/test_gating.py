from charmdb.gating import decide_optimizer_gate


def test_closed_evidence_gates_force_full_f3() -> None:
    decision = decide_optimizer_gate(False, 0, False)

    assert decision.decision == "FULL_F3_REQUIRED"
    assert not decision.probabilistic_gate_enabled
    assert not decision.early_stopping_enabled
    assert "full F3 evaluation remains mandatory" in decision.reasons


def test_adaptive_eligibility_requires_both_gates_and_infeasible_labels() -> None:
    no_negative_class = decide_optimizer_gate(True, 0, True)
    enabled = decide_optimizer_gate(True, 3, True)

    assert no_negative_class.decision == "FULL_F3_REQUIRED"
    assert enabled.decision == "ADAPTIVE_ELIGIBLE"
