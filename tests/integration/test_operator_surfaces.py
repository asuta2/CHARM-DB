from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from charmdb.api import app
from charmdb.config import get_settings
from charmdb.controller import apply_configuration, rollback_configuration
from charmdb.db import connect

pytestmark = pytest.mark.integration


def _configured() -> bool:
    return Path(".env").exists()


@pytest.mark.skipif(not _configured(), reason="integration environment not configured")
def test_manual_rollback_and_history_api_surfaces() -> None:
    settings = get_settings()
    application = apply_configuration(settings, {"random_page_cost": "3.9"})
    try:
        client = TestClient(app)
        response = client.post(
            f"/configurations/{application.application_id}/rollback",
            json={"reason": "operator API integration rollback"},
        )
        assert response.status_code == 200
        assert response.json()["restored"] == {"random_page_cost": "4"}

        drift = client.get("/drift-events", params={"limit": 1})
        assert drift.status_code == 200
        assert len(drift.json()) <= 1
        indexes = client.get("/indexes", params={"limit": 1})
        assert indexes.status_code == 200
        assert len(indexes.json()) <= 1
        gates = client.get("/experiments/gates")
        assert gates.status_code == 200
        assert len(gates.json()) == 7
        assert {item["gate"] for item in gates.json()} == {
            "measurement_validity",
            "paired_fidelity_gate",
            "calibration_gate",
            "coordination_execution_gate",
            "drift_execution_gate",
            "cost_measurement_gate",
            "full_pipeline_gate",
        }
        if indexes.json():
            candidate = client.get(f"/indexes/{indexes.json()[0]['candidate_id']}")
            assert candidate.status_code == 200
            assert "events" in candidate.json()
    finally:
        rollback_configuration(
            settings, application.application_id, "operator API integration final cleanup"
        )
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT setting FROM pg_settings WHERE name='random_page_cost'")
        assert cur.fetchone() == {"setting": "4"}
