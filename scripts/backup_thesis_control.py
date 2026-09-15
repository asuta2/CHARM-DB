"""Back up control data and verify it in an isolated, temporary control-server database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from charmdb.config import get_settings


def database_fingerprints(dsn: str) -> list[dict[str, Any]]:
    result = []
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute("SET TIME ZONE 'UTC'")
        tables = conn.execute(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog','information_schema') "
            "ORDER BY schemaname,tablename"
        ).fetchall()
        for schema, table in tables:
            digest = hashlib.sha256()
            count = 0
            query = sql.SQL(
                'SELECT row_to_json(t)::text FROM {}.{} t ORDER BY row_to_json(t)::text COLLATE "C"'
            ).format(sql.Identifier(schema), sql.Identifier(table))
            with conn.cursor(name="release_fingerprint") as cur:
                cur.execute(query)
                for (row,) in cur:
                    encoded = row.encode("utf-8")
                    digest.update(len(encoded).to_bytes(8, "big"))
                    digest.update(encoded)
                    count += 1
            result.append(
                {"schema": schema, "table": table, "rows": count, "sha256": digest.hexdigest()}
            )
    return result


def backup(output: Path) -> dict[str, Any]:
    settings = get_settings()
    params = conninfo_to_dict(settings.control_dsn)
    target = conninfo_to_dict(settings.target_dsn)
    if params.get("host", "localhost") not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Backup rehearsal requires a local control server")
    if all(params.get(k) == target.get(k) for k in ("host", "port", "dbname")):
        raise ValueError("Control and target must differ")
    if output.exists():
        raise ValueError("Choose a fresh backup output directory")
    output.mkdir(parents=True)
    tools = {name: shutil.which(name) for name in ("pg_dump", "pg_restore")}
    if not all(tools.values()):
        raise RuntimeError("PostgreSQL client tools are required")
    environment = dict(os.environ)
    for key, value in params.items():
        environment["PG" + key.upper() if key != "dbname" else "PGDATABASE"] = value
    environment["PGCONNECT_TIMEOUT"] = "10"

    def execute(name: str, args: list[str]) -> None:
        result = subprocess.run(
            [str(tools[name]), *args], env=environment, capture_output=True, check=False
        )
        if result.returncode:
            # Keep diagnostics local; never interpolate a DSN or credentials into exceptions.
            (output / f"{name}-error.log").write_bytes(result.stderr)
            raise RuntimeError(f"{name} failed; inspect its local error log")

    before = database_fingerprints(settings.control_dsn)
    dump = output / "control.dump"
    execute("pg_dump", ["--format=custom", "--no-owner", "--no-acl", "--file", str(dump)])
    execute("pg_restore", ["--list", str(dump)])
    temporary = "charmdb_d073_verify_" + uuid.uuid4().hex
    restored_dsn = make_conninfo(settings.control_dsn, dbname=temporary)
    created = False
    try:
        with psycopg.connect(settings.control_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(temporary))
            )
            created = True
        execute(
            "pg_restore",
            ["--exit-on-error", "--no-owner", "--no-acl", "--dbname", temporary, str(dump)],
        )
        restored = database_fingerprints(restored_dsn)
        after = database_fingerprints(settings.control_dsn)
        if before != restored or before != after:
            raise RuntimeError("Restored table fingerprints differ or source changed during backup")
    finally:
        if created:
            with psycopg.connect(settings.control_dsn, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(temporary))
                )
    with dump.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    result = {
        "decision": "D073",
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "status": "PASS",
        "source_database": params["dbname"],
        "dump_file": dump.name,
        "dump_bytes": dump.stat().st_size,
        "dump_sha256": digest,
        "temporary_database": temporary,
        "temporary_database_removed": True,
        "source_before_equals_after_equals_restored": True,
        "table_fingerprints": before,
        "scope": (
            "Local isolated logical restore; ownership/ACL/global roles not included; "
            "not a separate-host recovery test"
        ),
    }
    (output / "backup-verification.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = backup(args.output.resolve())
    print(json.dumps({k: v for k, v in result.items() if k != "table_fingerprints"}, indent=2))


if __name__ == "__main__":
    main()
