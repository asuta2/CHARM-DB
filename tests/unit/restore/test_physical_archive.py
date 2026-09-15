from pathlib import Path

import charmdb.restore.physical_archive as physical_archive


def test_physical_archive_filename_prefix_is_scale_neutral() -> None:
    assert physical_archive.PHYSICAL_ARCHIVE_FILENAME_PREFIX == "physical-canonical"


def test_restore_volume_targets_only_validated_named_volume(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], int]] = []

    def fake_docker(arguments: list[str], *, timeout: int = 30) -> str:
        calls.append((arguments, timeout))
        return ""

    monkeypatch.setattr(physical_archive, "run_docker", fake_docker)

    physical_archive._restore_volume(
        "charm_target_data",
        "sha256:image",
        tmp_path,
        "canonical.tar",
    )

    arguments, timeout = calls[0]
    assert arguments[:6] == [
        "run",
        "--rm",
        "--user",
        "0:0",
        "--mount",
        "type=volume,source=charm_target_data,target=/target",
    ]
    assert "readonly" in arguments[7]
    assert arguments[-3] == "sha256:image"
    assert "find /target -mindepth 1 -maxdepth 1" in arguments[-1]
    assert "tar -xf /backup/canonical.tar -C /target" in arguments[-1]
    assert timeout == 1800


def test_archive_volume_mounts_source_read_only(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_docker(arguments: list[str], *, timeout: int = 30) -> str:
        del timeout
        calls.append(arguments)
        return ""

    monkeypatch.setattr(physical_archive, "run_docker", fake_docker)

    physical_archive._archive_volume(
        "charm_target_data",
        "sha256:image",
        tmp_path,
        "canonical.tar",
    )

    arguments = calls[0]
    assert "type=volume,source=charm_target_data,target=/source,readonly" in arguments
    assert arguments[-5:] == ["-C", "/source", "-cf", "/backup/canonical.tar", "."]
