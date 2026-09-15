from charmdb.restore.capability import evaluate_restore_capabilities


def _evidence() -> dict[str, object]:
    return {
        "target_mount": {
            "Type": "volume",
            "Name": "charm_target_data",
            "Destination": "/var/lib/postgresql",
            "Driver": "local",
            "RW": True,
        },
        "docker_engine": {"storage_driver": "overlayfs"},
        "container_tools": {"tar": True, "sha256sum": True},
        "pg_dump": "C:/PostgreSQL/bin/pg_dump.exe",
        "pg_restore": "C:/PostgreSQL/bin/pg_restore.exe",
    }


def test_local_named_volume_qualifies_only_archive_and_logical_mechanisms() -> None:
    result = evaluate_restore_capabilities(_evidence())

    assert not result["copy-on-write"].eligible
    assert result["physical-archive"].eligible
    assert result["logical-restore"].eligible


def test_physical_archive_fails_closed_without_dedicated_volume() -> None:
    evidence = _evidence()
    evidence["target_mount"] = {
        "Type": "bind",
        "Name": "",
        "Destination": "/var/lib/postgresql",
        "Driver": "",
        "RW": True,
    }

    result = evaluate_restore_capabilities(evidence)

    assert not result["physical-archive"].eligible


def test_physical_archive_fails_closed_without_archive_tools() -> None:
    evidence = _evidence()
    evidence["container_tools"] = {"tar": True, "sha256sum": False}

    result = evaluate_restore_capabilities(evidence)

    assert not result["physical-archive"].eligible
