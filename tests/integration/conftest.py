"""Keep integration tests isolated from the research `.env` by default."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict

from charmdb.config import get_settings


@pytest.fixture(autouse=True)
def disposable_integration_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.getenv("CHARMDB_DISPOSABLE_INTEGRATION") != "1":
        pytest.skip("integration tests require explicit disposable-test configuration")
    required = {
        "CHARMDB_TEST_CONTROL_DSN": "CHARMDB_CONTROL_DSN",
        "CHARMDB_TEST_TARGET_DSN": "CHARMDB_TARGET_DSN",
        "CHARMDB_TEST_TARGET_PASSWORD": "CHARMDB_TARGET_PASSWORD",
        "CHARMDB_TEST_TARGET_DB": "CHARMDB_TARGET_DB",
        "CHARMDB_TEST_TARGET_HOST": "CHARMDB_TARGET_HOST",
        "CHARMDB_TEST_TARGET_PORT": "CHARMDB_TARGET_PORT",
        "CHARMDB_TEST_ARTIFACT_DIR": "CHARMDB_ARTIFACT_DIR",
    }
    values = {name: os.getenv(name) for name in required}
    if any(not value for value in values.values()):
        pytest.fail("disposable integration configuration is incomplete")
    control = conninfo_to_dict(values["CHARMDB_TEST_CONTROL_DSN"] or "")
    target = conninfo_to_dict(values["CHARMDB_TEST_TARGET_DSN"] or "")
    if not str(control.get("dbname", "")).startswith("charmdb_test_"):
        pytest.fail("integration control database must use a charmdb_test_ name")
    if not str(target.get("dbname", "")).startswith("charmdb_test_"):
        pytest.fail("integration target database must use a charmdb_test_ name")
    if control == target:
        pytest.fail("integration control and target must be distinct")
    project = os.getenv("COMPOSE_PROJECT_NAME", "")
    if not project.startswith("charmdb_test_"):
        pytest.fail("integration Compose project must use a charmdb_test_ name")
    artifact_path = Path(values["CHARMDB_TEST_ARTIFACT_DIR"] or "").resolve()
    disposable_root = Path(__file__).resolve().parents[2] / ".pytest-tmp-integration"
    if not artifact_path.is_relative_to(disposable_root.resolve()):
        pytest.fail("integration artifacts must be inside .pytest-tmp-integration")
    for test_name, runtime_name in required.items():
        monkeypatch.setenv(runtime_name, values[test_name] or "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
