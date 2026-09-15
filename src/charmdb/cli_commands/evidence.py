from __future__ import annotations

import json
from pathlib import Path

import typer

from charmdb.campaigns.primary import (
    PRIMARY_STAGE,
)
from charmdb.cli_commands import app
from charmdb.config import get_settings
from charmdb.reporting.evidence import export_v2_evidence_ledgers
from charmdb.reporting.integrity import build_evidence_manifest
from charmdb.reporting.secondary import export_secondary_report


@app.command("primary-report-secondary")
def v2_primary_report_secondary_command(
    source: Path | None = None, output: Path | None = None
) -> None:
    """Render D050 exploratory showcases from the terminal Wave A export."""
    artifact_root = (get_settings().artifact_dir / PRIMARY_STAGE).resolve()
    source_path = (source or artifact_root / "primary-analysis.json").resolve()
    output_dir = (output or artifact_root / "report-secondary").resolve()
    for label, selected in (("source", source_path), ("output", output_dir)):
        if selected != artifact_root and artifact_root not in selected.parents:
            raise typer.BadParameter(
                f"secondary report {label} must stay under the primary artifact root"
            )
    typer.echo(json.dumps(export_secondary_report(source_path, output_dir), indent=2))


@app.command("evidence-manifest")
def v2_evidence_manifest_command(root: Path | None = None, output: Path | None = None) -> None:
    artifact_root = get_settings().artifact_dir
    selected_root = root or artifact_root
    typer.echo(
        json.dumps(
            build_evidence_manifest(selected_root, output, artifact_root=artifact_root),
            indent=2,
        )
    )


@app.command("evidence-ledgers-export")
def v2_evidence_ledgers_export_command(output: Path | None = None) -> None:
    typer.echo(
        json.dumps(export_v2_evidence_ledgers(get_settings(), output), indent=2, default=str)
    )
