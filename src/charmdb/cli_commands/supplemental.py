from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Annotated

import typer

from charmdb.campaigns.h25 import (
    H25_MANIFEST,
    OWNER,
    abandon_h25_block,
    analyze_h25,
    create_h25_plan,
    export_h25_analysis,
    h25_history,
    h25_readiness,
    h25_step_dict,
    render_h25_report,
    run_h25_next,
)
from charmdb.cli_commands import app
from charmdb.config import get_settings


@app.command("h25-validate", help="Validate the supplemental H25 design and live readiness gates.")
def h25_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = H25_MANIFEST,
) -> None:
    result = h25_readiness(get_settings(), manifest)
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result["ready"]:
        raise typer.Exit(code=1)


@app.command("h25-create", help="Create the immutable 12-slot H25 campaign without starting it.")
def h25_create_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = H25_MANIFEST,
) -> None:
    campaign_id = create_h25_plan(get_settings(), manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("h25-run-next")
def h25_run_next_command(
    campaign_id: uuid.UUID,
    owner: str = OWNER,
    lease_seconds: int = 600,
) -> None:
    typer.echo(
        json.dumps(
            h25_step_dict(
                run_h25_next(get_settings(), campaign_id, owner=owner, lease_seconds=lease_seconds)
            ),
            indent=2,
            default=str,
        )
    )


@app.command("h25-run", help="Run H25 until completion, infrastructure pause, or max-steps.")
def h25_run_command(
    campaign_id: uuid.UUID,
    owner: str = OWNER,
    lease_seconds: int = 600,
    max_steps: int = 0,
) -> None:
    if max_steps < 0:
        raise typer.BadParameter("max-steps cannot be negative")
    completed = 0
    while max_steps == 0 or completed < max_steps:
        step = run_h25_next(get_settings(), campaign_id, owner=owner, lease_seconds=lease_seconds)
        typer.echo(json.dumps(h25_step_dict(step), default=str))
        completed += 1
        if step.action in {"observations-complete", "infrastructure-paused"}:
            break


@app.command(
    "h25-abandon",
    help="Retire a live H25 block without valid observations and stop its campaign.",
)
def h25_abandon_command(
    campaign_id: uuid.UUID,
    reason: Annotated[str, typer.Option("--reason")],
) -> None:
    typer.echo(
        json.dumps(abandon_h25_block(get_settings(), campaign_id, reason), indent=2, default=str)
    )


@app.command("h25-history")
def h25_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(h25_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("h25-analyze")
def h25_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(analyze_h25(get_settings(), campaign_id), indent=2, default=str))


@app.command("h25-export")
def h25_export_command(campaign_id: uuid.UUID, output: Path | None = None) -> None:
    typer.echo(
        json.dumps(export_h25_analysis(get_settings(), campaign_id, output), indent=2, default=str)
    )


@app.command("h25-report")
def h25_report_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(render_h25_report(get_settings(), campaign_id), indent=2, default=str))
