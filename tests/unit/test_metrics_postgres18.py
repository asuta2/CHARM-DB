from charmdb.metrics import SNAPSHOT_SQL


def test_snapshot_uses_postgresql_18_wal_columns() -> None:
    assert "wal_records" in SNAPSHOT_SQL
    assert "wal_buffers_full" in SNAPSHOT_SQL
    assert "wal_write_time" not in SNAPSHOT_SQL
