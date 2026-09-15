from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Annotated

import typer

from charmdb.analysis.windows import analyze_measurement_marker
from charmdb.campaigns.default_reference import (
    DEFAULT_REFERENCE_MANIFEST,
    FROZEN_PREFLIGHT_ID,
    analyze_default_reference,
    create_default_reference_plan,
    default_reference_history,
    default_reference_readiness,
    default_reference_step_dict,
    export_default_reference_analysis,
    run_default_reference_next,
)
from charmdb.campaigns.saturation import (
    PHASE1_MANIFEST,
    create_phase1_plan,
    phase1_analysis,
    phase1_history,
    run_phase1_next,
    saturation_step_dict,
)
from charmdb.campaigns.saturation_phase2 import (
    PHASE2_MANIFEST,
    create_phase2_plan,
    phase2_analysis,
    phase2_history,
    phase2_readiness,
    phase2_step_dict,
    run_phase2_next,
)
from charmdb.campaigns.screening import (
    SCREENING_MANIFEST,
    analyze_screening,
    create_screening_plan,
    export_screening_analysis,
    export_screening_design,
    run_screening_next,
    screening_design_summary,
    screening_history,
    screening_readiness,
    screening_step_dict,
)
from charmdb.campaigns.screening_recovery import (
    RECOVERY_MANIFEST,
    REMEDIATION_MANIFEST,
    analyze_screening_recovery,
    create_restore_stability_plan,
    create_screening_recovery_plan,
    create_target_database_remediation,
    export_screening_recovery_analysis,
    recovery_design_summary,
    restore_stability_history,
    restore_stability_readiness,
    run_restore_stability_next,
    run_screening_recovery_next,
    run_target_database_remediation,
    screening_recovery_history,
    screening_recovery_readiness,
    target_database_remediation_history,
    target_database_remediation_readiness,
)
from charmdb.campaigns.warmup_duration import (
    PILOT_MANIFEST,
    analyze_duration,
    analyze_warmup,
    create_pilot_plan,
    freeze_warmup,
    pilot_history,
    pilot_readiness,
    pilot_step_dict,
    run_pilot_next,
)
from charmdb.cli_commands import app
from charmdb.config import get_settings


@app.command("saturation-phase1-create")
def v2_saturation_phase1_create_command(
    manifest: Path = PHASE1_MANIFEST,
) -> None:
    campaign_id = create_phase1_plan(get_settings(), manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("saturation-phase1-run-next")
def v2_saturation_phase1_run_next_command(
    campaign_id: uuid.UUID,
    owner: str = "v2-saturation",
    lease_seconds: int = typer.Option(600, min=180, max=7200),
) -> None:
    typer.echo(
        json.dumps(
            saturation_step_dict(
                run_phase1_next(
                    get_settings(),
                    campaign_id,
                    owner=owner,
                    lease_seconds=lease_seconds,
                )
            ),
            indent=2,
            default=str,
        )
    )


@app.command("saturation-phase1-history")
def v2_saturation_phase1_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(phase1_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("saturation-phase1-analysis")
def v2_saturation_phase1_analysis_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(phase1_analysis(get_settings(), campaign_id), indent=2, default=str))


@app.command("saturation-phase2-create")
def v2_saturation_phase2_create_command(
    preflight_id: uuid.UUID,
    manifest: Path = PHASE2_MANIFEST,
) -> None:
    campaign_id = create_phase2_plan(get_settings(), preflight_id, manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("saturation-phase2-validate")
def v2_saturation_phase2_validate_command(
    preflight_id: uuid.UUID,
    manifest: Path = PHASE2_MANIFEST,
) -> None:
    typer.echo(
        json.dumps(
            phase2_readiness(get_settings(), preflight_id, manifest),
            indent=2,
            default=str,
        )
    )


@app.command("saturation-phase2-run-next")
def v2_saturation_phase2_run_next_command(
    campaign_id: uuid.UUID,
    owner: str = "v2-saturation-phase2",
    lease_seconds: int = typer.Option(600, min=180, max=7200),
) -> None:
    typer.echo(
        json.dumps(
            phase2_step_dict(
                run_phase2_next(
                    get_settings(),
                    campaign_id,
                    owner=owner,
                    lease_seconds=lease_seconds,
                )
            ),
            indent=2,
            default=str,
        )
    )


@app.command("saturation-phase2-history")
def v2_saturation_phase2_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(phase2_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("saturation-phase2-analysis")
def v2_saturation_phase2_analysis_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(phase2_analysis(get_settings(), campaign_id), indent=2, default=str))


@app.command("window-analyze")
def v2_window_analyze_command(
    marker: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
    prefix_seconds: int = typer.Option(300, min=30, max=3600),
    rolling_window_seconds: int = typer.Option(60, min=10, max=600),
) -> None:
    typer.echo(
        json.dumps(
            analyze_measurement_marker(
                marker,
                prefix_seconds=prefix_seconds,
                rolling_window_seconds=rolling_window_seconds,
            ),
            indent=2,
            default=str,
        )
    )


@app.command("warmup-duration-validate")
def v2_warmup_duration_validate_command(
    preflight_id: uuid.UUID,
    manifest: Path = PILOT_MANIFEST,
) -> None:
    typer.echo(
        json.dumps(
            pilot_readiness(get_settings(), preflight_id, manifest),
            indent=2,
            default=str,
        )
    )


@app.command("warmup-duration-create")
def v2_warmup_duration_create_command(
    preflight_id: uuid.UUID,
    manifest: Path = PILOT_MANIFEST,
) -> None:
    campaign_id = create_pilot_plan(get_settings(), preflight_id, manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("warmup-duration-run-next")
def v2_warmup_duration_run_next_command(
    campaign_id: uuid.UUID,
    owner: str = "v2-warmup-duration",
    lease_seconds: int = typer.Option(600, min=180, max=7200),
) -> None:
    typer.echo(
        json.dumps(
            pilot_step_dict(
                run_pilot_next(
                    get_settings(),
                    campaign_id,
                    owner=owner,
                    lease_seconds=lease_seconds,
                )
            ),
            indent=2,
            default=str,
        )
    )


@app.command("warmup-duration-history")
def v2_warmup_duration_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(pilot_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("warmup-analyze")
def v2_warmup_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(analyze_warmup(get_settings(), campaign_id), indent=2, default=str))


@app.command("warmup-freeze")
def v2_warmup_freeze_command(
    campaign_id: uuid.UUID,
    selected_warmup_seconds: int = typer.Option(min=0, max=1200),
    analysis_sha256: str = typer.Option(min=64, max=64),
    reason: str = typer.Option(min=1),
) -> None:
    typer.echo(
        json.dumps(
            freeze_warmup(
                get_settings(),
                campaign_id,
                selected_warmup_seconds,
                analysis_sha256,
                reason,
            ),
            indent=2,
            default=str,
        )
    )


@app.command("duration-analyze")
def v2_duration_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(analyze_duration(get_settings(), campaign_id), indent=2, default=str))


@app.command("default-reference-validate")
def v2_default_reference_validate_command(
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = DEFAULT_REFERENCE_MANIFEST,
) -> None:
    typer.echo(
        json.dumps(
            default_reference_readiness(get_settings(), preflight_id, manifest),
            indent=2,
            default=str,
        )
    )


@app.command("default-reference-create")
def v2_default_reference_create_command(
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = DEFAULT_REFERENCE_MANIFEST,
) -> None:
    campaign_id = create_default_reference_plan(get_settings(), preflight_id, manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("default-reference-run-next")
def v2_default_reference_run_next_command(
    campaign_id: uuid.UUID,
    owner: str = "v2-default-reference",
    lease_seconds: int = typer.Option(600, min=30, max=3600),
) -> None:
    typer.echo(
        json.dumps(
            default_reference_step_dict(
                run_default_reference_next(
                    get_settings(), campaign_id, owner=owner, lease_seconds=lease_seconds
                )
            ),
            indent=2,
            default=str,
        )
    )


@app.command("default-reference-history")
def v2_default_reference_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(default_reference_history(get_settings(), campaign_id), indent=2, default=str)
    )


@app.command("default-reference-analyze")
def v2_default_reference_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(analyze_default_reference(get_settings(), campaign_id), indent=2, default=str)
    )


@app.command("default-reference-export")
def v2_default_reference_export_command(
    campaign_id: uuid.UUID,
    output: Annotated[Path | None, typer.Option(file_okay=True, dir_okay=False)] = None,
) -> None:
    typer.echo(
        json.dumps(
            export_default_reference_analysis(get_settings(), campaign_id, output),
            indent=2,
        )
    )


@app.command("screening-design-validate")
def v2_screening_design_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = SCREENING_MANIFEST,
) -> None:
    typer.echo(json.dumps(screening_design_summary(manifest), indent=2))


@app.command("screening-design-export")
def v2_screening_design_export_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = SCREENING_MANIFEST,
    output: Annotated[Path | None, typer.Option(file_okay=True, dir_okay=False)] = None,
) -> None:
    typer.echo(json.dumps(export_screening_design(manifest, output), indent=2))


@app.command("screening-validate")
def v2_screening_validate_command(
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = SCREENING_MANIFEST,
) -> None:
    typer.echo(
        json.dumps(
            screening_readiness(get_settings(), preflight_id, manifest),
            indent=2,
            default=str,
        )
    )


@app.command("screening-create")
def v2_screening_create_command(
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = SCREENING_MANIFEST,
) -> None:
    campaign_id = create_screening_plan(get_settings(), preflight_id, manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("screening-run-next")
def v2_screening_run_next_command(
    campaign_id: uuid.UUID,
    owner: str = "v2-screening",
    lease_seconds: int = typer.Option(600, min=30, max=3600),
) -> None:
    typer.echo(
        json.dumps(
            screening_step_dict(
                run_screening_next(
                    get_settings(), campaign_id, owner=owner, lease_seconds=lease_seconds
                )
            ),
            indent=2,
            default=str,
        )
    )


@app.command("screening-history")
def v2_screening_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(screening_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("screening-analyze")
def v2_screening_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(analyze_screening(get_settings(), campaign_id), indent=2, default=str))


@app.command("screening-export")
def v2_screening_export_command(
    campaign_id: uuid.UUID,
    output: Annotated[Path | None, typer.Option(file_okay=True, dir_okay=False)] = None,
) -> None:
    typer.echo(json.dumps(export_screening_analysis(get_settings(), campaign_id, output), indent=2))


@app.command("screening-recovery-design-validate")
def v2_screening_recovery_design_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = RECOVERY_MANIFEST,
) -> None:
    typer.echo(json.dumps(recovery_design_summary(manifest), indent=2))


@app.command("screening-recovery-remediation-validate")
def v2_screening_recovery_remediation_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = REMEDIATION_MANIFEST,
) -> None:
    typer.echo(
        json.dumps(
            target_database_remediation_readiness(get_settings(), manifest),
            indent=2,
            default=str,
        )
    )


@app.command("screening-recovery-remediation-create")
def v2_screening_recovery_remediation_create_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = REMEDIATION_MANIFEST,
) -> None:
    remediation_id = create_target_database_remediation(get_settings(), manifest)
    typer.echo(json.dumps({"remediation_id": str(remediation_id), "status": "PLANNED"}, indent=2))


@app.command("screening-recovery-remediation-run")
def v2_screening_recovery_remediation_run_command(
    remediation_id: uuid.UUID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = REMEDIATION_MANIFEST,
) -> None:
    typer.echo(
        json.dumps(
            run_target_database_remediation(get_settings(), remediation_id, manifest),
            indent=2,
            default=str,
        )
    )


@app.command("screening-recovery-remediation-history")
def v2_screening_recovery_remediation_history_command(remediation_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(
            target_database_remediation_history(get_settings(), remediation_id),
            indent=2,
            default=str,
        )
    )


@app.command("screening-recovery-restore-validate")
def v2_screening_recovery_restore_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = RECOVERY_MANIFEST,
) -> None:
    typer.echo(
        json.dumps(
            restore_stability_readiness(get_settings(), manifest),
            indent=2,
            default=str,
        )
    )


@app.command("screening-recovery-restore-create")
def v2_screening_recovery_restore_create_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = RECOVERY_MANIFEST,
) -> None:
    validation_block_id = create_restore_stability_plan(get_settings(), manifest)
    typer.echo(
        json.dumps(
            {"validation_block_id": str(validation_block_id), "status": "PLANNED"},
            indent=2,
        )
    )


@app.command("screening-recovery-restore-run-next")
def v2_screening_recovery_restore_run_next_command(
    validation_block_id: uuid.UUID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = RECOVERY_MANIFEST,
) -> None:
    typer.echo(
        json.dumps(
            run_restore_stability_next(get_settings(), validation_block_id, manifest),
            indent=2,
            default=str,
        )
    )


@app.command("screening-recovery-restore-history")
def v2_screening_recovery_restore_history_command(validation_block_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(
            restore_stability_history(get_settings(), validation_block_id),
            indent=2,
            default=str,
        )
    )


@app.command("screening-recovery-validate")
def v2_screening_recovery_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = RECOVERY_MANIFEST,
) -> None:
    typer.echo(
        json.dumps(
            screening_recovery_readiness(get_settings(), manifest),
            indent=2,
            default=str,
        )
    )


@app.command("screening-recovery-create")
def v2_screening_recovery_create_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = RECOVERY_MANIFEST,
) -> None:
    campaign_id = create_screening_recovery_plan(get_settings(), manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("screening-recovery-run-next")
def v2_screening_recovery_run_next_command(
    campaign_id: uuid.UUID,
    owner: str = "v2-screening-recovery",
    lease_seconds: int = typer.Option(600, min=30, max=3600),
) -> None:
    typer.echo(
        json.dumps(
            screening_step_dict(
                run_screening_recovery_next(
                    get_settings(), campaign_id, owner=owner, lease_seconds=lease_seconds
                )
            ),
            indent=2,
            default=str,
        )
    )


@app.command("screening-recovery-history")
def v2_screening_recovery_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(screening_recovery_history(get_settings(), campaign_id), indent=2, default=str)
    )


@app.command("screening-recovery-analyze")
def v2_screening_recovery_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(analyze_screening_recovery(get_settings(), campaign_id), indent=2, default=str)
    )


@app.command("screening-recovery-export")
def v2_screening_recovery_export_command(
    campaign_id: uuid.UUID,
    output: Annotated[Path | None, typer.Option(file_okay=True, dir_okay=False)] = None,
) -> None:
    typer.echo(
        json.dumps(
            export_screening_recovery_analysis(get_settings(), campaign_id, output), indent=2
        )
    )
