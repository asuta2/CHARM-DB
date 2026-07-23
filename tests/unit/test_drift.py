from river.drift import ADWIN

from charmdb.drift import classify_drift


def test_adwin_detects_persistent_step_change() -> None:
    detector = ADWIN(delta=0.01, clock=1, min_window_length=3, grace_period=8)
    detected = []
    for index, value in enumerate([0.01] * 20 + [2.0] * 20):
        detector.update(value)
        if detector.drift_detected:
            detected.append(index)
    assert detected
    assert detected[0] >= 20


def test_drift_classification_distinguishes_modes() -> None:
    assert classify_drift([0.1, 0.8, 0.1], 0.5) == "TRANSIENT"
    assert classify_drift([0.1, 0.1, 0.9, 0.8], 0.5) == "SUDDEN"
    assert classify_drift([0.1, 0.2, 0.35, 0.55, 0.75, 0.85], 0.7) == "GRADUAL"
    assert classify_drift([0.1, 0.2, 0.3], 0.5) is None
