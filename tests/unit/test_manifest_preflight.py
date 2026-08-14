from charmdb.manifest_preflight import (
    CANONICAL_SNAPSHOT_FILENAME,
    assess_manifest_evidence,
    normalize_schema_dump,
)


def test_canonical_snapshot_filename_is_scale_neutral() -> None:
    assert CANONICAL_SNAPSHOT_FILENAME == "pgbench-canonical.dump"


def test_schema_dump_normalization_removes_nondeterministic_headers() -> None:
    first = """-- Dumped from database version 18.1
-- Dumped by pg_dump version 18.4
\\restrict abc123
CREATE TABLE public.pgbench_accounts (aid integer);
\\unrestrict abc123
"""
    second = """-- Dumped from database version 18.1\r
-- Dumped by pg_dump version 18.4\r
\\restrict different\r
CREATE TABLE public.pgbench_accounts (aid integer);\r
\\unrestrict different\r
"""

    assert normalize_schema_dump(first) == normalize_schema_dump(second)
    assert normalize_schema_dump(first) == "CREATE TABLE public.pgbench_accounts (aid integer);\n"


def test_manifest_evidence_fails_closed_without_exact_f3_reference() -> None:
    references = [
        {
            "warmup_seconds": 5,
            "measurement_seconds": 30,
            "concurrency": 4,
            "completed_trials": 12,
        }
    ]
    unresolved = assess_manifest_evidence(references, 120, 600, 4)

    assert len(unresolved) == 2
    assert "logical dataset snapshot restore has not been validated" in unresolved
    assert any("fewer than five" in item for item in unresolved)
