"""Create and read-back verify local Zip64 release archives, without changing inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

EVIDENCE_MANIFEST_SHA = "8fcfee8eca196a966f86d16ab6e2b7ea91b3f4bfd401ac360dd7e11b7c807e96"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_paths(root: Path) -> list[str]:
    names = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
        )
        .decode("utf-8")
        .split("\0")
    )
    selected = []
    for name in sorted(set(names) - {""}):
        path = Path(name)
        if path.parts[0] in {"artifacts", "artifacts-newpc"}:
            continue
        if any(p.startswith(".") for p in path.parts) and name not in {
            ".env.example",
            ".gitignore",
        }:
            continue
        if path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".local"}:
            continue
        if (root / name).is_file():
            selected.append(name)
    return selected


def verify_archive(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("RELEASE-MANIFEST.json"))
        expected = {row["path"] for row in manifest["files"]} | {"RELEASE-MANIFEST.json"}
        if len(archive.namelist()) != len(expected) or set(archive.namelist()) != expected:
            raise ValueError("Archive member inventory mismatch")
        for number, row in enumerate(manifest["files"], 1):
            if archive.getinfo(row["path"]).file_size != row["bytes"]:
                raise ValueError(f"Archive size mismatch: {row['path']}")
            with archive.open(row["path"]) as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != row["sha256"]:
                raise ValueError(f"Archive hash mismatch: {row['path']}")
            if number % 500 == 0:
                print(f"Verified archive members {number}/{len(manifest['files'])}", flush=True)
    return manifest


def build(root: Path, output: Path, kind: str) -> dict[str, Any]:
    root, output = root.resolve(), output.resolve()
    if output.is_relative_to(root) or output.exists() or output.with_suffix(".partial").exists():
        raise ValueError("Output must be new and outside the input root")
    expected: dict[str, dict[str, Any]] = {}
    if kind == "evidence":
        manifest_path = root / "evidence-sha256-manifest.json"
        if digest(manifest_path) != EVIDENCE_MANIFEST_SHA:
            raise ValueError("D071 evidence trust anchor differs")
        frozen = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = {row["relative_path"]: row for row in frozen["files"]}
        names = sorted([*expected, manifest_path.name])
        actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        if actual != set(names):
            raise ValueError("Evidence tree membership changed")
    elif kind == "source":
        names = source_paths(root)
    else:
        names = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    if not names:
        raise ValueError("Refusing empty release")
    total = sum((root / name).stat().st_size for name in names)
    output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output.parent).free < total + 10 * 1024**3:
        raise ValueError("Insufficient space for archive plus 10 GiB reserve")
    manifest: dict[str, Any] = {"decision": "D073", "kind": kind, "files": []}
    if kind == "source":
        manifest["git_head"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        manifest["snapshot_semantics"] = (
            "Current tracked and nonignored untracked files; "
            "local credentials/caches/artifacts excluded"
        )
    partial = output.with_suffix(".partial")
    with zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for number, name in enumerate(names, 1):
            path = (root / name).resolve()
            if (
                not path.is_relative_to(root)
                or ".." in Path(name).parts
                or Path(name).is_absolute()
            ):
                raise ValueError(f"Input escapes root: {name}")
            sha = hashlib.sha256()
            size = 0
            with path.open("rb") as source, archive.open(name, "w", force_zip64=True) as dest:
                while block := source.read(4 * 1024**2):
                    dest.write(block)
                    sha.update(block)
                    size += len(block)
            value = sha.hexdigest()
            if name in expected:
                row = expected[name]
                if size != row["byte_size"] or value != row["sha256"]:
                    raise ValueError(f"Source evidence changed: {name}")
            manifest["files"].append({"path": name, "bytes": size, "sha256": value})
            if number % 500 == 0:
                print(f"Archived {number}/{len(names)} files", flush=True)
        archive.writestr(
            "RELEASE-MANIFEST.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
    verified = verify_archive(partial)
    if verified != manifest:
        raise ValueError("Archive manifest changed")
    partial.rename(output)
    receipt = {
        "decision": "D073",
        "kind": kind,
        "archive": str(output),
        "status": "PASS",
        "archive_sha256": digest(output),
        "archive_bytes": output.stat().st_size,
        "verified_members": len(names),
        "input_bytes": total,
        "verification": "Every archive member read back and matched by size and SHA-256",
        "storage_limitation": "Local same-disk archive; not an off-host backup",
    }
    output.with_suffix(".receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--kind", choices=["source", "evidence", "submission"])
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        result = verify_archive(args.verify)
        print(f"PASS: {len(result['files'])} archive members")
    elif args.root and args.output and args.kind:
        print(json.dumps(build(args.root, args.output, args.kind), indent=2))
    else:
        parser.error("Supply --verify or --root, --output and --kind")


if __name__ == "__main__":
    main()
