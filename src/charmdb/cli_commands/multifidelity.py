from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Annotated

import typer

from charmdb.analysis.multifidelity import PRIMARY_CAMPAIGN_ID, execute_phase_a
from charmdb.campaigns.multifidelity import (
    MULTIFIDELITY_MANIFEST,
    analyze_phase_b,
    create_phase_b_plan,
    export_phase_b_analysis,
    phase_b_fixture_remediation_status,
    phase_b_history,
    phase_b_readiness,
    phase_b_step_dict,
    remediate_phase_b_test_fixture,
    run_phase_b_next,
    validate_phase_b_fixture_remediation,
)
from charmdb.cli_commands import app
from charmdb.config import get_settings


@app.command("multifidelity-phase-a")
def v2_multifidelity_phase_a_command(
    campaign_id: uuid.UUID = PRIMARY_CAMPAIGN_ID,
    output: Path | None = None,
) -> None:
    """Execute the D052 retrospective analysis without running a benchmark."""
    typer.echo(json.dumps(execute_phase_a(get_settings(), campaign_id, output), indent=2))


@app.command("multifidelity-phase-b-validate")
def v2_multifidelity_phase_b_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = MULTIFIDELITY_MANIFEST,
) -> None:
    result = phase_b_readiness(get_settings(), manifest)
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result["ready"]:
        raise typer.Exit(code=1)


@app.command("multifidelity-phase-b-create")
def v2_multifidelity_phase_b_create_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = MULTIFIDELITY_MANIFEST,
) -> None:
    campaign_id = create_phase_b_plan(get_settings(), manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("multifidelity-phase-b-run-next")
def v2_multifidelity_phase_b_run_next_command(
    campaign_id: uuid.UUID,
    owner: str = "v2-multifidelity-phase-b",
    lease_seconds: int = 600,
) -> None:
    step = run_phase_b_next(get_settings(), campaign_id, owner=owner, lease_seconds=lease_seconds)
    typer.echo(json.dumps(phase_b_step_dict(step), indent=2, default=str))


@app.command("multifidelity-phase-b-run")
def v2_multifidelity_phase_b_run_command(
    campaign_id: uuid.UUID,
    owner: str = "v2-multifidelity-phase-b",
    lease_seconds: int = 600,
    max_steps: int = 0,
) -> None:
    """Run durable Phase B slots until completion, infrastructure pause, or a bound."""
    if max_steps < 0:
        raise typer.BadParameter("max-steps cannot be negative")
    completed = 0
    while max_steps == 0 or completed < max_steps:
        step = run_phase_b_next(
            get_settings(), campaign_id, owner=owner, lease_seconds=lease_seconds
        )
        typer.echo(json.dumps(phase_b_step_dict(step), default=str))
        completed += 1
        if step.action in {"observations-complete", "infrastructure-paused"}:
            break


@app.command("multifidelity-phase-b-history")
def v2_multifidelity_phase_b_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(phase_b_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("multifidelity-phase-b-analyze")
def v2_multifidelity_phase_b_analyze_command(campaign_id: uuid.UUID) -> None:
    """Authenticate and persist the terminal Phase B result."""
    typer.echo(json.dumps(analyze_phase_b(get_settings(), campaign_id), indent=2, default=str))


@app.command("multifidelity-phase-b-export")
def v2_multifidelity_phase_b_export_command(
    campaign_id: uuid.UUID, output: Path | None = None
) -> None:
    """Export the hash-authenticated terminal Phase B analysis."""
    typer.echo(
        json.dumps(
            export_phase_b_analysis(get_settings(), campaign_id, output),
            indent=2,
            default=str,
        )
    )


@app.command("multifidelity-phase-b-fixture-status")
def v2_multifidelity_phase_b_fixture_status_command(campaign_id: uuid.UUID) -> None:
    """Verify whether the failed restore is solely the known index-test fixture."""
    typer.echo(
        json.dumps(
            phase_b_fixture_remediation_status(get_settings(), campaign_id),
            indent=2,
            default=str,
        )
    )


@app.command("multifidelity-phase-b-remediate-fixture")
def v2_multifidelity_phase_b_remediate_fixture_command(campaign_id: uuid.UUID) -> None:
    """Remove only the exactly recognized test fixture and reconcile the failed attempt."""
    typer.echo(
        json.dumps(
            remediate_phase_b_test_fixture(get_settings(), campaign_id),
            indent=2,
            default=str,
        )
    )


@app.command("multifidelity-phase-b-validate-remediation")
def v2_multifidelity_phase_b_validate_remediation_command(campaign_id: uuid.UUID) -> None:
    """Verify baseline fingerprints after the guarded test-fixture cleanup."""
    typer.echo(
        json.dumps(
            validate_phase_b_fixture_remediation(get_settings(), campaign_id),
            indent=2,
            default=str,
        )
    )
