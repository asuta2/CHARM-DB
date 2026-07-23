from pathlib import Path

import pytest

from charmdb.analysis import analysis_readiness, generate_analysis_readiness
from charmdb.config import get_settings
from charmdb.experiments import experiment_registry_status, register_experiment_matrix

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not Path(".env").exists(), reason="integration environment not configured")
def test_full_experiment_matrix_registration_is_idempotent() -> None:
    settings = get_settings()
    first = register_experiment_matrix(settings)
    status_after_first_registration = experiment_registry_status(settings)
    repeated = register_experiment_matrix(settings)
    status = experiment_registry_status(settings)

    assert (
        first
        == repeated
        == {
            "groups": 19,
            "comparison_groups": 7,
            "ablations": 12,
            "arms": 168,
        }
    )
    assert status == status_after_first_registration
    assert len(status) == 19
    assert sum(int(group["arm_count"]) for group in status) == 168

    readiness = analysis_readiness(settings)
    completed_arms = sum(int(group["completed_arms"]) for group in status)
    expected_readiness = "COMPLETE" if completed_arms == 168 else "INCOMPLETE"
    assert readiness.status == expected_readiness
    assert readiness.completed_arms == completed_arms
    assert len(readiness.missing_arms) == 168 - completed_arms
    result = generate_analysis_readiness(settings)
    assert result.output_path.is_file()
    assert result.readiness == readiness
