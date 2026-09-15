from __future__ import annotations

import json
import shutil
import signal
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from threading import Event
from typing import Annotated

import typer

from charmdb.analysis.multifidelity import PRIMARY_CAMPAIGN_ID, execute_phase_a
from charmdb.analysis.rehearsal import (
    DEFAULT_REHEARSAL_DIRNAME,
    REHEARSAL_SCENARIOS,
    run_analysis_rehearsal,
)
from charmdb.analysis.windows import analyze_measurement_marker
from charmdb.apply_best import (
    APPLY_BEST_MANIFEST,
    activate_apply_best,
    apply_best_readiness,
    apply_best_status,
    prepare_apply_best,
    rollback_apply_best,
    run_apply_best_recovery_test,
)
from charmdb.bootstrap import (
    bootstrap_report_dict,
    run_bootstrap_preflight,
    write_bootstrap_report,
)
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
from charmdb.config import get_settings
from charmdb.controller import (
    discover_knobs,
    rollback_configuration,
)
from charmdb.db import apply_migrations, connect
from charmdb.optimization.design import (
    PRIMARY_MANIFEST,
    PRIMARY_WAVE_B_MANIFEST,
    export_primary_design,
    primary_design_summary,
)
from charmdb.reporting.evidence import export_v2_evidence_ledgers
from charmdb.reporting.integrity import build_evidence_manifest
from charmdb.reporting.secondary import export_secondary_report
from charmdb.restore.candidate import (
    approve_candidate_baseline,
    candidate_restore_history,
    candidate_restore_result_dict,
    ensure_candidate_dataset_restored,
    register_candidate_baseline,
)
from charmdb.restore.capability import (
    assess_restore_capabilities,
    capability_assessment_dict,
    restore_capability_history,
)
from charmdb.restore.physical_archive import (
    create_physical_archive,
    physical_archive_dict,
    physical_archive_history,
)
from charmdb.worker import (
    campaign_status,
    control_campaign,
    create_campaign,
    create_health_trial,
    create_v2_baseline_benchmark_trial,
    run_once,
    run_worker_service,
    service_result_json,
    trial_history,
)
from charmdb.worker import (
    result_json as worker_result_json,
)
from charmdb.workload import seed_pgbench

app = typer.Typer(
    no_args_is_help=True,
    help="CHARM-DB research controller",
    pretty_exceptions_show_locals=False,
)





@app.command()
def migrate(directory: Path = Path("migrations")) -> None:
    applied = apply_migrations(get_settings().control_dsn, directory)
    typer.echo(json.dumps({"applied": applied}))



@app.command()
def seed(scale: int = typer.Option(10, min=1, max=10_000)) -> None:
    typer.echo(seed_pgbench(get_settings(), scale))



@app.command()
def smoke() -> None:
    settings = get_settings()
    checks: dict[str, object] = {"pgbench": shutil.which("pgbench") is not None}
    for name, dsn in (("control", settings.control_dsn), ("target", settings.target_dsn)):
        with connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT version() AS version, current_database() AS database")
            checks[name] = cur.fetchone()
    checks["target_allowed"] = True
    typer.echo(json.dumps(checks, indent=2, default=str))



@app.command("bootstrap-preflight")
def v2_bootstrap_preflight_command(
    phase: Annotated[str, typer.Option(help="preinitialize or postinitialize")] = "preinitialize",
    output: Annotated[
        Path | None, typer.Option(help="Optional immutable JSON evidence path")
    ] = None,
    freshness_report: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Required preinitialize report for postinitialize validation",
        ),
    ] = None,
) -> None:
    if phase not in {"preinitialize", "postinitialize"}:
        raise typer.BadParameter("phase must be preinitialize or postinitialize")
    report = run_bootstrap_preflight(
        get_settings(),
        phase=phase,  # type: ignore[arg-type]
        freshness_report=freshness_report,
    )
    payload = bootstrap_report_dict(report)
    if output is not None:
        payload["evidence_path"] = str(write_bootstrap_report(report, output))
    typer.echo(json.dumps(payload, indent=2, default=str))
    if not report.passed:
        raise typer.Exit(code=2)



@app.command("candidate-baseline-register")
def v2_candidate_baseline_register_command(
    preflight_id: uuid.UUID,
    validation_id: uuid.UUID,
    restore_mechanism: str = "logical-restore",
) -> None:
    baseline = register_candidate_baseline(
        get_settings(),
        preflight_id,
        validation_id,
        restore_mechanism=restore_mechanism,
    )
    typer.echo(json.dumps(asdict(baseline), indent=2, default=str))



@app.command("candidate-baseline-approve")
def v2_candidate_baseline_approve_command(
    baseline_id: uuid.UUID,
    reason: str,
) -> None:
    baseline = approve_candidate_baseline(get_settings(), baseline_id, reason)
    typer.echo(json.dumps(asdict(baseline), indent=2, default=str))



@app.command("candidate-trial-create")
def v2_candidate_trial_create_command(
    campaign_id: uuid.UUID,
    preflight_id: uuid.UUID,
    idempotency_key: str,
    evidence_role: str = "INFRASTRUCTURE",
    evaluation_role: str = "DEFAULT_CONTROL",
    seed: int = 20260713,
    warmup_seconds: int = typer.Option(2, min=0, max=600),
    duration_seconds: int = typer.Option(5, min=1, max=3600),
    concurrency: int = typer.Option(4, min=1, max=128),
    fidelity: int = typer.Option(3, min=2, max=4),
    p99_slo_ms: float = typer.Option(20.0, min=0.001),
    max_attempts: int = typer.Option(3, min=1, max=20),
    restore_mechanism: str = "logical-restore",
    physical_archive_id: uuid.UUID | None = None,
) -> None:
    trial_id = create_v2_baseline_benchmark_trial(
        get_settings(),
        campaign_id,
        preflight_id,
        evidence_role,
        seed,
        idempotency_key,
        warmup_seconds,
        duration_seconds,
        concurrency,
        fidelity,
        p99_slo_ms,
        max_attempts,
        evaluation_role,
        restore_mechanism,
        physical_archive_id,
    )
    typer.echo(json.dumps({"trial_id": str(trial_id), "state": "CREATED"}, indent=2))



@app.command("candidate-restore-history")
def v2_candidate_restore_history_command(
    trial_id: uuid.UUID | None = None,
    limit: int = typer.Option(100, min=1, max=1000),
) -> None:
    typer.echo(
        json.dumps(
            candidate_restore_history(get_settings(), trial_id, limit),
            indent=2,
            default=str,
        )
    )



@app.command("candidate-restore-ensure")
def v2_candidate_restore_ensure_command(
    trial_id: uuid.UUID,
    preflight_id: uuid.UUID,
    restore_mechanism: str = "logical-restore",
    physical_archive_id: uuid.UUID | None = None,
) -> None:
    typer.echo(
        json.dumps(
            candidate_restore_result_dict(
                ensure_candidate_dataset_restored(
                    get_settings(),
                    trial_id,
                    preflight_id,
                    restore_mechanism=restore_mechanism,
                    physical_archive_id=physical_archive_id,
                )
            ),
            indent=2,
            default=str,
        )
    )



@app.command("restore-capabilities")
def v2_restore_capabilities_command() -> None:
    typer.echo(
        json.dumps(
            capability_assessment_dict(assess_restore_capabilities(get_settings())),
            indent=2,
            default=str,
        )
    )



@app.command("physical-archive-create")
def v2_physical_archive_create_command(
    baseline_id: uuid.UUID,
    assessment_id: uuid.UUID,
) -> None:
    typer.echo(
        json.dumps(
            physical_archive_dict(
                create_physical_archive(get_settings(), baseline_id, assessment_id)
            ),
            indent=2,
            default=str,
        )
    )



@app.command("restore-capability-history")
def v2_restore_capability_history_command(
    limit: int = typer.Option(100, min=1, max=1000),
) -> None:
    typer.echo(json.dumps(restore_capability_history(get_settings(), limit), indent=2, default=str))



@app.command("physical-archive-history")
def v2_physical_archive_history_command(
    limit: int = typer.Option(100, min=1, max=1000),
) -> None:
    typer.echo(json.dumps(physical_archive_history(get_settings(), limit), indent=2, default=str))



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



@app.command("multifidelity-phase-a")
def v2_multifidelity_phase_a_command(
    campaign_id: uuid.UUID = PRIMARY_CAMPAIGN_ID,
    output: Path | None = None,
) -> None:
    """Execute the D052 retrospective analysis without running a benchmark."""
    typer.echo(json.dumps(execute_phase_a(get_settings(), campaign_id, output), indent=2))



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



@app.command("knobs-discover")
def knobs_discover() -> None:
    typer.echo(json.dumps(discover_knobs(get_settings()), indent=2, default=str))



@app.command("knobs-rollback")
def knobs_rollback(application_id: uuid.UUID, reason: str = "manual rollback") -> None:
    restored = rollback_configuration(get_settings(), application_id, reason)
    typer.echo(json.dumps({"application_id": str(application_id), "restored": restored}, indent=2))



@app.command("campaign-create")
def campaign_create(
    name: str,
    mode: str = "MONITOR",
    failure_limit: int = typer.Option(5, min=1, max=100),
) -> None:
    campaign_id = create_campaign(get_settings(), name, mode, {}, {}, failure_limit, actor="cli")
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))



def _control_campaign_command(campaign_id: uuid.UUID, action: str, reason: str) -> None:
    typer.echo(
        json.dumps(
            control_campaign(get_settings(), campaign_id, action, reason, actor="cli"), indent=2
        )
    )



@app.command("campaign-pause")
def campaign_pause(campaign_id: uuid.UUID, reason: str = "operator pause") -> None:
    _control_campaign_command(campaign_id, "pause", reason)



@app.command("campaign-resume")
def campaign_resume(campaign_id: uuid.UUID, reason: str = "operator resume") -> None:
    _control_campaign_command(campaign_id, "resume", reason)



@app.command("campaign-stop")
def campaign_stop(campaign_id: uuid.UUID, reason: str = "operator stop") -> None:
    _control_campaign_command(campaign_id, "stop", reason)



@app.command("campaign-emergency-stop")
def campaign_emergency_stop(
    campaign_id: uuid.UUID, reason: str = "operator emergency stop"
) -> None:
    _control_campaign_command(campaign_id, "emergency-stop", reason)



@app.command("campaign-status")
def campaign_status_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(campaign_status(get_settings(), campaign_id), indent=2, default=str))



@app.command("trial-health-create")
def trial_health_create(
    campaign_id: uuid.UUID,
    idempotency_key: str,
    seed: int = 20260713,
    max_attempts: int = typer.Option(3, min=1, max=20),
) -> None:
    trial_id = create_health_trial(get_settings(), campaign_id, seed, idempotency_key, max_attempts)
    typer.echo(json.dumps({"trial_id": str(trial_id), "state": "CREATED"}, indent=2))



@app.command("trial-history")
def trial_history_command(trial_id: uuid.UUID) -> None:
    typer.echo(json.dumps(trial_history(get_settings(), trial_id), indent=2, default=str))



@app.command("worker-run-once")
def worker_run_once(
    owner: str | None = None,
    lease_seconds: int = typer.Option(30, min=5, max=7200),
    stop_after_state: str | None = None,
    hold_seconds: int = typer.Option(0, min=0, max=3600),
    campaign_id: uuid.UUID | None = None,
) -> None:
    result = run_once(
        get_settings(),
        owner=owner,
        lease_seconds=lease_seconds,
        stop_after_state=stop_after_state,
        campaign_id=campaign_id,
    )
    typer.echo(worker_result_json(result))
    if hold_seconds:
        time.sleep(hold_seconds)



@app.command("worker-run")
def worker_run(
    owner: str | None = None,
    lease_seconds: int = typer.Option(30, min=5, max=7200),
    poll_seconds: float = typer.Option(1.0, min=0.05, max=60.0),
    campaign_id: uuid.UUID | None = None,
    idle_exit_seconds: float | None = typer.Option(None, min=0.0),
    max_trials: int | None = typer.Option(None, min=1),
) -> None:
    shutdown = Event()
    previous_handlers: dict[signal.Signals, object] = {}

    def request_shutdown(_signum: int, _frame: object) -> None:
        shutdown.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)
    try:
        result = run_worker_service(
            get_settings(),
            owner=owner,
            lease_seconds=lease_seconds,
            poll_seconds=poll_seconds,
            campaign_id=campaign_id,
            shutdown_event=shutdown,
            idle_exit_seconds=idle_exit_seconds,
            max_trials=max_trials,
            event_callback=lambda event: typer.echo(json.dumps(event, default=str)),
        )
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]
    typer.echo(service_result_json(result))



if __name__ == "__main__":
    app()
