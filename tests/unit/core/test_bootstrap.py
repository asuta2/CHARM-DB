from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from charmdb.bootstrap import (
    EXPECTED_IMAGE,
    CommandResult,
    _migration_hashes,
    bootstrap_report_dict,
    run_bootstrap_preflight,
    write_bootstrap_report,
)


def _repository(tmp_path: Any) -> Any:
    (tmp_path / "src/charmdb").mkdir(parents=True)
    (tmp_path / "migrations").mkdir()
    (tmp_path / "experiments/thesis/manifests").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("locked\n", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "src/charmdb/module.py").write_text("VALUE = 1\n", encoding="utf-8")
    for number in range(1, 26):
        (tmp_path / "migrations" / f"{number:03d}_migration.sql").write_text(
            f"SELECT {number};\n", encoding="utf-8"
        )
    manifest = {
        "protocol_id": "thesis-protocol-v2",
        "schema_version": 1,
        "stage": "new-machine-bootstrap",
        "status": "draft",
        "evidence_role": "INFRASTRUCTURE",
        "operator_triggered": True,
        "artifact_root": "artifacts-newpc/v2/bootstrap",
        "candidate_restore_gate": "under-validation",
        "prerequisites": {},
        "unresolved_decisions": ["test fixture"],
    }
    (tmp_path / "experiments/thesis/manifests/new-machine-bootstrap.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return tmp_path


def _settings(root: Any, *, isolated: bool = True) -> Any:
    artifact_dir = root / "artifacts-newpc/v2" if isolated else root / "artifacts"
    return SimpleNamespace(
        artifact_dir=artifact_dir,
        control_dsn="postgresql://control_user:secret@127.0.0.1:5432/control_v2",
        target_dsn="postgresql://target_user:secret@127.0.0.1:55432/target_v2",
        target_host="127.0.0.1",
        target_port=55432,
        target_db="target_v2",
        target_user="target_user",
    )


def _compose_payload() -> dict[str, Any]:
    return {
        "services": {
            "target-postgres": {
                "image": EXPECTED_IMAGE,
                "cpus": 4,
                "mem_limit": str(4 * 1024**3),
                "shm_size": str(1024**3),
                "pids_limit": 512,
                "command": ["postgres", "-c", "shared_preload_libraries=pg_stat_statements"],
                "environment": {
                    "POSTGRES_INITDB_ARGS": "--data-checksums",
                    "POSTGRES_PASSWORD": "must-not-be-persisted",
                },
            }
        },
        "volumes": {"charm_target_data": {"name": "charm_target_data"}},
    }


def _runner(*, initialized: bool = False) -> Any:
    def run(command: list[str]) -> CommandResult:
        key = tuple(command)
        if key == ("uv", "--version"):
            return CommandResult(0, "uv 0.test", "")
        if key == ("git", "rev-parse", "HEAD"):
            return CommandResult(0, "a" * 40, "")
        if key == ("git", "status", "--porcelain", "--untracked-files=no"):
            return CommandResult(0, "", "")
        if key == ("docker", "compose", "config", "--format", "json"):
            return CommandResult(0, json.dumps(_compose_payload()), "")
        if key == ("docker", "version", "--format", "{{json .}}"):
            return CommandResult(
                0,
                json.dumps(
                    {
                        "Client": {"Version": "29.1.2"},
                        "Server": {"Version": "29.1.2", "Os": "linux", "Arch": "amd64"},
                    }
                ),
                "",
            )
        if key == ("docker", "compose", "version", "--short"):
            return CommandResult(0, "2.40.3", "")
        if key == ("docker", "volume", "inspect", "charm_target_data"):
            return CommandResult(0 if initialized else 1, "[]" if initialized else "", "")
        if key == ("docker", "compose", "ps", "-q", "target-postgres"):
            return CommandResult(0, "container-id" if initialized else "", "")
        raise AssertionError(f"unexpected command: {command}")

    return run


def _control_probe(root: Any) -> Any:
    from charmdb.bootstrap import _migration_hashes

    hashes = _migration_hashes(root)
    return lambda _settings: {
        "database": "control_v2",
        "version": "PostgreSQL 18",
        "migrations": hashes,
    }


def test_preinitialize_report_passes_without_leaking_compose_secrets(tmp_path: Any) -> None:
    root = _repository(tmp_path)
    report = run_bootstrap_preflight(
        _settings(root),
        root=root,
        runner=_runner(),
        control_probe=_control_probe(root),
    )

    assert report.passed
    assert report.safe_to_initialize
    serialized = json.dumps(bootstrap_report_dict(report))
    assert "must-not-be-persisted" not in serialized
    assert "secret" not in serialized


def test_migration_hashes_match_apply_migrations_text_normalization(tmp_path: Any) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    path = migrations / "001_example.sql"
    path.write_bytes(b"SELECT 1;\r\n")

    expected = hashlib.sha256(b"SELECT 1;\n").hexdigest()

    assert _migration_hashes(tmp_path) == {"001_example": expected}


def test_legacy_artifact_root_fails_closed(tmp_path: Any) -> None:
    root = _repository(tmp_path)
    report = run_bootstrap_preflight(
        _settings(root, isolated=False),
        root=root,
        runner=_runner(),
        control_probe=_control_probe(root),
    )

    assert not report.passed
    assert not report.safe_to_initialize
    check = next(item for item in report.checks if item.name == "artifact-root-isolation")
    assert check.status == "FAIL"


def test_conflicting_target_dsn_and_fields_fail_closed(tmp_path: Any) -> None:
    root = _repository(tmp_path)
    settings = _settings(root)
    settings.target_db = "different_target"

    report = run_bootstrap_preflight(
        settings,
        root=root,
        runner=_runner(),
        control_probe=_control_probe(root),
    )

    assert not report.safe_to_initialize
    check = next(item for item in report.checks if item.name == "target-dsn-consistency")
    assert check.status == "FAIL"


def test_postinitialize_requires_valid_freshness_report(tmp_path: Any) -> None:
    root = _repository(tmp_path)
    settings = _settings(root)
    preinitialize = run_bootstrap_preflight(
        settings,
        root=root,
        runner=_runner(),
        control_probe=_control_probe(root),
    )
    evidence_path = root / "artifacts-newpc/v2/bootstrap/preinitialize.json"
    write_bootstrap_report(preinitialize, evidence_path)

    def target_probe(_settings: Any) -> dict[str, Any]:
        return {
            "database": "target_v2",
            "version": "PostgreSQL 18",
            "data_checksums": "on",
            "extensions": ["pg_stat_statements", "pg_buffercache", "pg_prewarm"],
        }

    report = run_bootstrap_preflight(
        settings,
        root=root,
        phase="postinitialize",
        freshness_report=evidence_path,
        runner=_runner(initialized=True),
        control_probe=_control_probe(root),
        target_probe=target_probe,
    )

    assert report.passed
    assert not report.safe_to_initialize


def test_evidence_writer_refuses_overwrite_and_outside_path(tmp_path: Any) -> None:
    root = _repository(tmp_path)
    report = run_bootstrap_preflight(
        _settings(root),
        root=root,
        runner=_runner(),
        control_probe=_control_probe(root),
    )
    output = root / "artifacts-newpc/v2/bootstrap/report.json"

    assert write_bootstrap_report(report, output) == output.resolve()
    with pytest.raises(FileExistsError):
        write_bootstrap_report(report, output)
    with pytest.raises(ValueError, match="configured artifact root"):
        write_bootstrap_report(report, root / "artifacts/report.json")


def test_external_artifact_destination_passes_isolation_and_writer(tmp_path: Any) -> None:
    root = _repository(tmp_path / "repository")
    settings = _settings(root)
    settings.artifact_dir = tmp_path / "external-evidence"
    report = run_bootstrap_preflight(
        settings,
        root=root,
        runner=_runner(),
        control_probe=_control_probe(root),
    )
    assert report.passed
    output = settings.artifact_dir / "bootstrap/report.json"
    assert write_bootstrap_report(report, output) == output.resolve()
