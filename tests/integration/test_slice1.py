import os
from pathlib import Path

import pytest

import charmdb.controller as controller
from charmdb.config import get_settings
from charmdb.db import connect

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not (os.getenv("CHARMDB_TARGET_DSN") or Path(".env").exists()),
    reason="integration environment not configured",
)
def test_control_and_target_are_separate_and_ready() -> None:
    settings = get_settings()
    assert settings.control_dsn != settings.target_dsn
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_database() AS db, current_setting('server_version') AS version")
        row = cur.fetchone()
        assert row is not None
        assert row["db"] == settings.target_db
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('charm_control.campaigns') AS relation")
        row = cur.fetchone()
        assert row is not None and row["relation"] is not None


@pytest.mark.skipif(
    not (os.getenv("CHARMDB_TARGET_DSN") or Path(".env").exists()),
    reason="integration environment not configured",
)
def test_failed_verification_automatically_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    original_read = controller._read_active
    calls = 0

    def fail_first_verification(settings_arg: object, names: set[str]) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"random_page_cost": "unexpected"}
        return original_read(settings, names)

    monkeypatch.setattr(controller, "_read_active", fail_first_verification)
    with pytest.raises(RuntimeError, match="active settings differ"):
        controller.apply_configuration(settings, {"random_page_cost": "3.8"})

    assert original_read(settings, {"random_page_cost"}) == {"random_page_cost": "4"}
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT a.status, count(r.rollback_id) AS rollbacks
            FROM charm_control.configuration_applications a
            LEFT JOIN charm_control.rollbacks r USING(application_id)
            WHERE a.requested_settings = '{"random_page_cost": "3.8"}'::jsonb
            GROUP BY a.application_id ORDER BY a.created_at DESC LIMIT 1"""
        )
        row = cur.fetchone()
        assert row == {"status": "ROLLED_BACK", "rollbacks": 1}
