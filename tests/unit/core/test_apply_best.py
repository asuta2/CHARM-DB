from copy import deepcopy
from pathlib import Path

import pytest

from charmdb.apply_best import (
    APPLY_BEST_MANIFEST,
    _canonical_sha256,
    apply_best_manifest_payload,
    champion_configuration,
    operator_time_estimate,
    validate_apply_best_manifest,
)


def test_apply_best_manifest_freezes_champion_without_authorizing_activation() -> None:
    _, payload = apply_best_manifest_payload()
    configuration = champion_configuration(payload)

    assert payload["champion"]["treatment"] == "E"
    assert payload["recovery_validation_authorized"] is True
    assert payload["persistent_activation_authorized"] is False
    assert payload["authorization_contract"]["manifest_does_not_authorize_activation"] is True
    assert configuration == {
        "checkpoint_completion_target": "0.936696",
        "checkpoint_timeout": "1430",
        "effective_cache_size": "465506",
        "max_parallel_workers_per_gather": "3",
        "max_wal_size": "633",
        "random_page_cost": "3.655316",
        "shared_buffers": "182848",
        "work_mem": "18022",
    }
    assert _canonical_sha256(configuration) == (
        "96627a9e7e5b6037170796fb283c5397f4bcacf20801aa39a7992a130f1d7a62"
    )
    assert APPLY_BEST_MANIFEST.is_file()


def test_apply_best_manifest_rejects_activation_authorization() -> None:
    _, payload = apply_best_manifest_payload()
    tampered = deepcopy(payload)
    tampered["persistent_activation_authorized"] = True

    with pytest.raises(ValueError, match="must not authorize"):
        validate_apply_best_manifest(tampered)


def test_operator_time_estimate_uses_nearest_rank_and_frozen_allowances() -> None:
    _, payload = apply_best_manifest_payload()

    result = operator_time_estimate([2.0, 4.0, 6.0, 8.0], [3.0, 5.0, 7.0, 9.0], payload)

    assert result["apply_duration_percentile_seconds"] == 8.0
    assert result["rollback_duration_percentile_seconds"] == 9.0
    assert result["activation_operator_estimate_seconds"] == 38
    assert result["emergency_rollback_operator_estimate_seconds"] == 39
    assert result["full_reversible_operator_window_seconds"] == 107
    assert result["full_reversible_operator_window_minutes"] == 107 / 60


def test_apply_best_migrations_freeze_singleton_and_verified_recovery_retry() -> None:
    migrations = Path(__file__).resolve().parents[3] / "migrations"
    migration_042 = (migrations / "042_v2_apply_best.sql").read_text(
        encoding="utf-8"
    )
    migration_043 = (migrations / "043_v2_apply_best_recovery_retry.sql").read_text(
        encoding="utf-8"
    )
    migration_044 = (migrations / "044_v2_apply_best_activation_recovery.sql").read_text(
        encoding="utf-8"
    )

    assert "v2_apply_best_single_deployment_idx" in migration_042
    assert "OLD.status='FAILED' AND NEW.status='RECOVERY_TESTING'" in migration_043
    assert "a.status='ROLLED_BACK' AND r.verified" in migration_043
    assert "OLD.status IN ('ACTIVATING','FAILED') AND NEW.status='ROLLING_BACK'" in migration_044
    assert "OLD.authorization_recorded_at IS NOT NULL" in migration_044
