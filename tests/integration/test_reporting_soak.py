import json
import os
from pathlib import Path

import pytest

from charmdb.config import get_settings
from charmdb.db import apply_migrations
from charmdb.reporting import generate_report
from charmdb.soak import run_soak_test

pytestmark = pytest.mark.integration


def _configured() -> bool:
    return bool(os.getenv("CHARMDB_TARGET_DSN") or Path(".env").exists())


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_report_exports_persisted_evidence(tmp_path: Path) -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    result = generate_report(settings, tmp_path)
    assert result.markdown_path.exists()
    assert result.csv_path.exists()
    assert result.json_path.exists()
    assert result.manifest_path.exists()
    payload = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert payload["campaign_count"] == result.campaigns
    assert payload["trial_count"] == result.trials
    assert "not a statistical significance claim" in payload["evidence_warning"]
    assert "multi_objective_recommendations" in payload
    assert "optimizer_gate_decisions" in payload
    assert "experiment_groups" in payload
    assert "experiment_arms" in payload
    assert "experiment_arm_events" in payload
    assert "experiment_budget_entries" in payload
    assert "experiment_search_recommendations" in payload
    assert "experiment_manifest_preflights" in payload
    assert "experiment_manifest_reviews" in payload
    assert "experiment_arm_dataset_restores" in payload
    assert "experiment_dataset_restore_validations" in payload
    assert "experiment_f3_reference_blocks" in payload
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_primary_keys"]["pareto_snapshots"] == "snapshot_id"
    assert manifest["source_primary_keys"]["experiment_budget_entries"] == "budget_entry_id"
    assert (
        manifest["source_primary_keys"]["experiment_search_recommendations"] == "recommendation_id"
    )
    assert manifest["source_primary_keys"]["experiment_manifest_preflights"] == "preflight_id"
    assert (
        manifest["source_primary_keys"]["experiment_dataset_restore_validations"] == "validation_id"
    )
    assert manifest["source_primary_keys"]["experiment_f3_reference_blocks"] == "block_id"
    assert manifest["source_primary_keys"]["experiment_manifest_reviews"] == "review_id"
    assert manifest["source_primary_keys"]["experiment_arm_dataset_restores"] == "arm_id"


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_two_second_soak_records_actual_elapsed_health_trials() -> None:
    settings = get_settings()
    apply_migrations(settings.control_dsn)
    result = run_soak_test(settings, "2s", interval_seconds=0.1, lease_seconds=30)
    assert result.requested_seconds == 2
    assert result.elapsed_seconds >= 2
    assert result.completed_trials >= 1
    assert result.failed_trials == 0
