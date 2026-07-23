from pathlib import Path

from charmdb.provenance import source_tree_sha256


def test_source_tree_digest_is_deterministic_and_content_sensitive(tmp_path: Path) -> None:
    (tmp_path / "src" / "charmdb").mkdir(parents=True)
    (tmp_path / "migrations").mkdir()
    for name in ("pyproject.toml", "uv.lock", "docker-compose.yml"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    source = tmp_path / "src" / "charmdb" / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "migrations" / "001.sql").write_text("SELECT 1;\n", encoding="utf-8")

    first = source_tree_sha256(tmp_path)
    repeated = source_tree_sha256(tmp_path)
    source.write_text("VALUE = 2\n", encoding="utf-8")

    assert first == repeated
    assert len(first) == 64
    assert source_tree_sha256(tmp_path) != first
