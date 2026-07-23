from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


def connect(dsn: str) -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=False)


def apply_migrations(dsn: str, directory: Path = Path("migrations")) -> list[str]:
    applied: list[str] = []
    with connect(dsn) as conn:
        for path in sorted(directory.glob("*.sql")):
            version = path.stem
            body = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(body.encode()).hexdigest()
            with conn.cursor() as cur:
                cur.execute(
                    """CREATE SCHEMA IF NOT EXISTS charm_control;
                    CREATE TABLE IF NOT EXISTS charm_control.schema_migrations (
                        version text PRIMARY KEY,
                        applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                        sha256 text NOT NULL
                    )"""
                )
                cur.execute(
                    "SELECT sha256 FROM charm_control.schema_migrations WHERE version = %s",
                    (version,),
                )
                existing = cur.fetchone()
                if existing:
                    if existing["sha256"] != digest:
                        raise RuntimeError(f"migration {version} changed after application")
                    continue
                cur.execute(body)
                cur.execute(
                    "INSERT INTO charm_control.schema_migrations(version, sha256) VALUES (%s, %s)",
                    (version, digest),
                )
                applied.append(version)
        conn.commit()
    return applied
