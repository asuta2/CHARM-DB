from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Annotated

import typer

from charmdb.apply_best import (
    APPLY_BEST_MANIFEST,
    activate_apply_best,
    apply_best_readiness,
    apply_best_status,
    prepare_apply_best,
    rollback_apply_best,
    run_apply_best_recovery_test,
)
from charmdb.campaigns.f4 import (
    F4_MANIFEST,
    analyze_f4,
    create_f4_plan,
    export_f4_analysis,
    f4_history,
    f4_readiness,
    f4_step_dict,
    render_f4_report,
    run_f4_next,
)
from charmdb.cli_commands import app
from charmdb.config import get_settings


@app.command("apply-best-validate")
def v2_apply_best_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = APPLY_BEST_MANIFEST,
) -> None:
    """Validate champion E provenance, target safety, recovery state, and timing."""
    result = apply_best_readiness(get_settings(), manifest)
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result["preparation_ready"]:
        raise typer.Exit(code=1)


@app.command("apply-best-prepare")
def v2_apply_best_prepare_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = APPLY_BEST_MANIFEST,
) -> None:
    """Create the singleton apply-best ledger without changing PostgreSQL."""
    settings = get_settings()
    deployment_id = prepare_apply_best(settings, manifest)
    status = apply_best_status(settings, deployment_id, manifest)["deployment"]["status"]
    typer.echo(
        json.dumps(
            {"deployment_id": str(deployment_id), "status": status, "target_changed": False},
            indent=2,
        )
    )


@app.command("apply-best-recovery-test")
def v2_apply_best_recovery_test_command(
    deployment_id: uuid.UUID,
    confirm_transient_change: Annotated[
        bool,
        typer.Option(
            "--confirm-transient-change",
            help="Acknowledge one restart/apply/verify/rollback recovery cycle.",
        ),
    ] = False,
) -> None:
    """Apply champion E transiently, verify it, and restore the exact snapshot."""
    if not confirm_transient_change:
        raise typer.BadParameter("--confirm-transient-change is required")
    typer.echo(
        json.dumps(
            run_apply_best_recovery_test(get_settings(), deployment_id),
            indent=2,
            default=str,
        )
    )


@app.command("apply-best-status")
def v2_apply_best_status_command(deployment_id: uuid.UUID | None = None) -> None:
    """Audit durable workflow state against the live target."""
    typer.echo(json.dumps(apply_best_status(get_settings(), deployment_id), indent=2, default=str))


@app.command("apply-best-activate")
def v2_apply_best_activate_command(
    deployment_id: uuid.UUID,
    decision_id: Annotated[str, typer.Option("--decision-id")],
    actor: Annotated[str, typer.Option("--actor")],
    statement: Annotated[str, typer.Option("--statement")],
    confirmed_configuration_sha256: Annotated[
        str, typer.Option("--confirmed-configuration-sha256")
    ],
) -> None:
    """Persist explicit authorization, then activate and verify champion E."""
    typer.echo(
        json.dumps(
            activate_apply_best(
                get_settings(),
                deployment_id,
                decision_id=decision_id,
                actor=actor,
                statement=statement,
                confirmed_configuration_sha256=confirmed_configuration_sha256,
            ),
            indent=2,
            default=str,
        )
    )


@app.command("apply-best-rollback")
def v2_apply_best_rollback_command(
    deployment_id: uuid.UUID,
    reason: Annotated[str, typer.Option("--reason")],
) -> None:
    """Restore the exact pre-activation snapshot and verify target health."""
    typer.echo(
        json.dumps(
            rollback_apply_best(get_settings(), deployment_id, reason),
            indent=2,
            default=str,
        )
    )


@app.command("f4-validate")
def v2_f4_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = F4_MANIFEST,
) -> None:
    """Validate the P010 F4 design and live readiness gates."""
    result = f4_readiness(get_settings(), manifest)
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result["ready"]:
        raise typer.Exit(code=1)


@app.command("f4-create")
def v2_f4_create_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = F4_MANIFEST,
) -> None:
    """Create the immutable 20-slot F4 campaign without starting it."""
    campaign_id = create_f4_plan(get_settings(), manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("f4-run-next")
def v2_f4_run_next_command(
    campaign_id: uuid.UUID,
    owner: str = "v2-f4-confirmation",
    lease_seconds: int = 600,
) -> None:
    typer.echo(
        json.dumps(
            f4_step_dict(
                run_f4_next(get_settings(), campaign_id, owner=owner, lease_seconds=lease_seconds)
            ),
            indent=2,
            default=str,
        )
    )


@app.command("f4-run")
def v2_f4_run_command(
    campaign_id: uuid.UUID,
    owner: str = "v2-f4-confirmation",
    lease_seconds: int = 600,
    max_steps: int = 0,
) -> None:
    """Run F4 until completion, infrastructure pause, or a caller-supplied bound."""
    if max_steps < 0:
        raise typer.BadParameter("max-steps cannot be negative")
    completed = 0
    while max_steps == 0 or completed < max_steps:
        step = run_f4_next(get_settings(), campaign_id, owner=owner, lease_seconds=lease_seconds)
        typer.echo(json.dumps(f4_step_dict(step), default=str))
        completed += 1
        if step.action in {"observations-complete", "infrastructure-paused"}:
            break


@app.command("f4-history")
def v2_f4_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(f4_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("f4-analyze")
def v2_f4_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(analyze_f4(get_settings(), campaign_id), indent=2, default=str))


@app.command("f4-export")
def v2_f4_export_command(campaign_id: uuid.UUID, output: Path | None = None) -> None:
    typer.echo(
        json.dumps(export_f4_analysis(get_settings(), campaign_id, output), indent=2, default=str)
    )


@app.command("f4-report")
def v2_f4_report_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(render_f4_report(get_settings(), campaign_id), indent=2, default=str))
