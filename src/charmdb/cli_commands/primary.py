from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Annotated

import typer

from charmdb.analysis.rehearsal import (
    DEFAULT_REHEARSAL_DIRNAME,
    REHEARSAL_SCENARIOS,
    run_analysis_rehearsal,
)
from charmdb.campaigns.default_reference import (
    FROZEN_PREFLIGHT_ID,
)
from charmdb.campaigns.primary import (
    PRIMARY_STAGE,
    analyze_primary,
    create_primary_plan,
    export_primary_analysis,
    export_primary_report,
    primary_history,
    primary_readiness,
    primary_step_dict,
    run_primary_next,
)
from charmdb.campaigns.primary_wave_b import (
    analyze_final_five_seed,
    analyze_wave_b,
    create_wave_b_plan,
    export_final_five_seed_analysis,
    export_final_five_seed_report,
    export_wave_b_analysis,
    export_wave_b_design,
    export_wave_b_report,
    reconcile_wave_b_interruption,
    run_wave_b_next,
    wave_b_design_summary,
    wave_b_history,
    wave_b_interruption_status,
    wave_b_readiness,
    wave_b_step_dict,
)
from charmdb.campaigns.restore_soak import (
    create_primary_restore_soak,
    primary_restore_soak_history,
    primary_restore_soak_readiness,
    run_primary_restore_soak_next,
)
from charmdb.cli_commands import app
from charmdb.config import get_settings
from charmdb.optimization.design import (
    PRIMARY_MANIFEST,
    PRIMARY_WAVE_B_MANIFEST,
    export_primary_design,
    primary_design_summary,
)


@app.command("primary-design-validate")
def v2_primary_design_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_MANIFEST,
) -> None:
    typer.echo(json.dumps(primary_design_summary(manifest), indent=2))


@app.command("primary-design-export")
def v2_primary_design_export_command(
    output: Path | None = None,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_MANIFEST,
) -> None:
    typer.echo(json.dumps(export_primary_design(manifest, output), indent=2))


@app.command("primary-validate")
def v2_primary_validate_command(
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_MANIFEST,
) -> None:
    typer.echo(
        json.dumps(
            primary_readiness(get_settings(), preflight_id, manifest),
            indent=2,
            default=str,
        )
    )


@app.command("primary-restore-soak-validate")
def v2_primary_restore_soak_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_MANIFEST,
) -> None:
    typer.echo(
        json.dumps(
            primary_restore_soak_readiness(get_settings(), manifest),
            indent=2,
            default=str,
        )
    )


@app.command("primary-restore-soak-create")
def v2_primary_restore_soak_create_command(
    repetitions: int = 15,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_MANIFEST,
) -> None:
    restore_soak_id = create_primary_restore_soak(get_settings(), repetitions, manifest)
    typer.echo(
        json.dumps(
            {
                "restore_soak_id": str(restore_soak_id),
                "status": "PLANNED",
                "required_repetitions": repetitions,
            },
            indent=2,
        )
    )


@app.command("primary-restore-soak-run")
def v2_primary_restore_soak_run_command(
    restore_soak_id: uuid.UUID,
    max_steps: int = 0,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_MANIFEST,
) -> None:
    if max_steps < 0:
        raise typer.BadParameter("max-steps cannot be negative")
    completed = 0
    while max_steps == 0 or completed < max_steps:
        step = run_primary_restore_soak_next(get_settings(), restore_soak_id, manifest)
        typer.echo(json.dumps(step, default=str))
        completed += 1
        if step["action"] in {"soak-complete", "already-terminal"}:
            break


@app.command("primary-restore-soak-history")
def v2_primary_restore_soak_history_command(restore_soak_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(
            primary_restore_soak_history(get_settings(), restore_soak_id),
            indent=2,
            default=str,
        )
    )


@app.command("primary-create")
def v2_primary_create_command(
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_MANIFEST,
) -> None:
    campaign_id = create_primary_plan(get_settings(), preflight_id, manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id)}, indent=2))


@app.command("primary-run-next")
def v2_primary_run_next_command(
    campaign_id: uuid.UUID,
    owner: str = "v2-primary",
    lease_seconds: int = 600,
) -> None:
    typer.echo(
        json.dumps(
            primary_step_dict(
                run_primary_next(
                    get_settings(), campaign_id, owner=owner, lease_seconds=lease_seconds
                )
            ),
            indent=2,
            default=str,
        )
    )


@app.command("primary-run")
def v2_primary_run_command(
    campaign_id: uuid.UUID,
    owner: str = "v2-primary",
    lease_seconds: int = 600,
    max_steps: int = 0,
) -> None:
    """Run durable primary slots until completion, infrastructure pause, or a step bound."""
    if max_steps < 0:
        raise typer.BadParameter("max-steps cannot be negative")
    completed = 0
    while max_steps == 0 or completed < max_steps:
        step = run_primary_next(
            get_settings(), campaign_id, owner=owner, lease_seconds=lease_seconds
        )
        payload = primary_step_dict(step)
        typer.echo(json.dumps(payload, default=str))
        completed += 1
        if step.action in {"observations-complete", "infrastructure-paused"}:
            break


@app.command("primary-history")
def v2_primary_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(primary_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("primary-analyze")
def v2_primary_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(analyze_primary(get_settings(), campaign_id), indent=2, default=str))


@app.command("primary-export")
def v2_primary_export_command(campaign_id: uuid.UUID, output: Path | None = None) -> None:
    typer.echo(json.dumps(export_primary_analysis(get_settings(), campaign_id, output), indent=2))


@app.command("primary-report")
def v2_primary_report_command(campaign_id: uuid.UUID, output: Path | None = None) -> None:
    """Render the frozen Wave A tables and figures from a terminal analysis."""
    typer.echo(json.dumps(export_primary_report(get_settings(), campaign_id, output), indent=2))


@app.command("primary-wave-b-design-validate")
def v2_primary_wave_b_design_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_WAVE_B_MANIFEST,
) -> None:
    """Validate the frozen, result-blind two-seed Wave B design."""
    typer.echo(json.dumps(wave_b_design_summary(manifest), indent=2))


@app.command("primary-wave-b-design-export")
def v2_primary_wave_b_design_export_command(
    output: Path | None = None,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_WAVE_B_MANIFEST,
) -> None:
    typer.echo(json.dumps(export_wave_b_design(manifest, output), indent=2))


@app.command("primary-wave-b-validate")
def v2_primary_wave_b_validate_command(
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_WAVE_B_MANIFEST,
) -> None:
    typer.echo(
        json.dumps(
            wave_b_readiness(get_settings(), preflight_id, manifest),
            indent=2,
            default=str,
        )
    )


@app.command("primary-wave-b-create")
def v2_primary_wave_b_create_command(
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_WAVE_B_MANIFEST,
) -> None:
    """Create exactly one Wave B campaign without running an observation."""
    campaign_id = create_wave_b_plan(get_settings(), preflight_id, manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "observations_run": 0}, indent=2))


@app.command("primary-wave-b-run-next")
def v2_primary_wave_b_run_next_command(
    campaign_id: uuid.UUID,
    owner: str = "v2-primary-wave-b",
    lease_seconds: int = 600,
) -> None:
    typer.echo(
        json.dumps(
            wave_b_step_dict(
                run_wave_b_next(
                    get_settings(), campaign_id, owner=owner, lease_seconds=lease_seconds
                )
            ),
            indent=2,
            default=str,
        )
    )


@app.command("primary-wave-b-run")
def v2_primary_wave_b_run_command(
    campaign_id: uuid.UUID,
    owner: str = "v2-primary-wave-b",
    lease_seconds: int = 600,
    max_steps: int = 0,
) -> None:
    """Run Wave B externally until completion, infrastructure pause, or a step bound."""
    if max_steps < 0:
        raise typer.BadParameter("max-steps cannot be negative")
    completed = 0
    while max_steps == 0 or completed < max_steps:
        step = run_wave_b_next(
            get_settings(), campaign_id, owner=owner, lease_seconds=lease_seconds
        )
        typer.echo(json.dumps(wave_b_step_dict(step), default=str))
        completed += 1
        if step.action in {"observations-complete", "infrastructure-paused"}:
            break


@app.command("primary-wave-b-history")
def v2_primary_wave_b_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(wave_b_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("primary-wave-b-interruption-status")
def v2_primary_wave_b_interruption_status_command(
    campaign_id: uuid.UUID, trial_id: uuid.UUID
) -> None:
    """Fail-closed audit of one stale Wave B measurement interrupted by a host restart."""
    typer.echo(
        json.dumps(
            wave_b_interruption_status(get_settings(), campaign_id, trial_id),
            indent=2,
            default=str,
        )
    )


@app.command("primary-wave-b-reconcile-interruption")
def v2_primary_wave_b_reconcile_interruption_command(
    campaign_id: uuid.UUID,
    trial_id: uuid.UUID,
    reason: str = "host power interruption during Wave B measurement",
) -> None:
    """Rollback and retain one proven interrupted Wave B attempt for exact retry."""
    typer.echo(
        json.dumps(
            reconcile_wave_b_interruption(get_settings(), campaign_id, trial_id, reason=reason),
            indent=2,
            default=str,
        )
    )


@app.command("primary-wave-b-analyze")
def v2_primary_wave_b_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(analyze_wave_b(get_settings(), campaign_id), indent=2, default=str))


@app.command("primary-wave-b-export")
def v2_primary_wave_b_export_command(campaign_id: uuid.UUID, output: Path | None = None) -> None:
    typer.echo(json.dumps(export_wave_b_analysis(get_settings(), campaign_id, output), indent=2))


@app.command("primary-wave-b-report")
def v2_primary_wave_b_report_command(campaign_id: uuid.UUID, output: Path | None = None) -> None:
    typer.echo(json.dumps(export_wave_b_report(get_settings(), campaign_id, output), indent=2))


@app.command("primary-final-analyze")
def v2_primary_final_analyze_command(wave_b_campaign_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(
            analyze_final_five_seed(get_settings(), wave_b_campaign_id),
            indent=2,
            default=str,
        )
    )


@app.command("primary-final-export")
def v2_primary_final_export_command(
    wave_b_campaign_id: uuid.UUID, output: Path | None = None
) -> None:
    typer.echo(
        json.dumps(
            export_final_five_seed_analysis(get_settings(), wave_b_campaign_id, output),
            indent=2,
        )
    )


@app.command("primary-final-report")
def v2_primary_final_report_command(
    wave_b_campaign_id: uuid.UUID, output: Path | None = None
) -> None:
    typer.echo(
        json.dumps(
            export_final_five_seed_report(get_settings(), wave_b_campaign_id, output),
            indent=2,
        )
    )


@app.command("primary-analysis-rehearsal")
def v2_primary_analysis_rehearsal_command(output: Path | None = None) -> None:
    """Prove the Wave A analysis and report pipeline on synthetic ledgers.

    This is result-blind INFRASTRUCTURE work: it never reads or creates a
    primary campaign, never contacts the target database, and writes only
    clearly labelled synthetic artifacts.
    """
    settings = get_settings()
    directory = output or settings.artifact_dir / PRIMARY_STAGE / DEFAULT_REHEARSAL_DIRNAME
    summary = run_analysis_rehearsal(directory, REHEARSAL_SCENARIOS)
    typer.echo(json.dumps(summary, indent=2, default=str))
    if not summary["passed"]:
        raise typer.Exit(code=1)
