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

from charmdb.analysis import analysis_readiness, generate_analysis_readiness
from charmdb.calibration import result_json as calibration_result_json
from charmdb.calibration import run_calibration_pilot
from charmdb.config import get_settings
from charmdb.controller import (
    apply_configuration,
    discover_knobs,
    result_json,
    rollback_configuration,
)
from charmdb.coordination import coordination_history
from charmdb.db import apply_migrations, connect
from charmdb.drift import result_json as drift_result_json
from charmdb.drift import run_drift_experiment
from charmdb.experiment_execution import (
    experiment_arm_history,
    finalize_experiment_arm,
    prepare_experiment_arm,
    prerequisite_gate_status,
    record_experiment_trial_budget,
)
from charmdb.experiments import experiment_registry_status, register_experiment_matrix
from charmdb.fidelity import result_json as fidelity_result_json
from charmdb.fidelity import run_fidelity_pilot
from charmdb.gating import optimizer_gate_status
from charmdb.indexing import result_json as index_result_json
from charmdb.indexing import run_index_experiment, seed_index_fixture
from charmdb.manifest_builder import (
    draft_search_manifest,
    generate_search_manifest,
    review_search_manifest,
)
from charmdb.manifest_builder import (
    result_json as manifest_result_json,
)
from charmdb.manifest_preflight import run_manifest_preflight, validate_dataset_restore
from charmdb.multiobjective import (
    champion_history,
    multiobjective_result_dict,
    pareto_history,
    run_multiobjective_campaign,
    verify_recommendation,
)
from charmdb.operations import drift_history, index_candidate_history, index_history
from charmdb.optimizer import observations_json, run_single_objective_campaign
from charmdb.reference_block import run_f3_reference_block
from charmdb.reporting import generate_report
from charmdb.search_execution import progress_json, run_search_arm, start_or_resume_search_arm
from charmdb.soak import run_soak_test, soak_result_dict
from charmdb.v2.bootstrap import (
    bootstrap_report_dict,
    run_bootstrap_preflight,
    write_bootstrap_report,
)
from charmdb.v2.candidate_restore import (
    approve_candidate_baseline,
    candidate_restore_history,
    candidate_restore_result_dict,
    ensure_candidate_dataset_restored,
    register_candidate_baseline,
)
from charmdb.v2.default_reference import (
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
from charmdb.v2.evidence_exports import export_v2_evidence_ledgers
from charmdb.v2.evidence_integrity import build_evidence_manifest
from charmdb.v2.f4_confirmation import (
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
from charmdb.v2.multifidelity_phase_a import PRIMARY_CAMPAIGN_ID, execute_phase_a
from charmdb.v2.multifidelity_phase_b import (
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
from charmdb.v2.physical_archive import (
    create_physical_archive,
    physical_archive_dict,
    physical_archive_history,
)
from charmdb.v2.primary_design import (
    PRIMARY_MANIFEST,
    PRIMARY_WAVE_B_MANIFEST,
    export_primary_design,
    primary_design_summary,
)
from charmdb.v2.primary_execution import (
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
from charmdb.v2.primary_rehearsal import (
    DEFAULT_REHEARSAL_DIRNAME,
    REHEARSAL_SCENARIOS,
    run_analysis_rehearsal,
)
from charmdb.v2.primary_reliability import (
    create_primary_restore_soak,
    primary_restore_soak_history,
    primary_restore_soak_readiness,
    run_primary_restore_soak_next,
)
from charmdb.v2.primary_wave_b import (
    analyze_final_five_seed,
    analyze_wave_b,
    create_wave_b_plan,
    export_final_five_seed_analysis,
    export_final_five_seed_report,
    export_wave_b_analysis,
    export_wave_b_design,
    export_wave_b_report,
    run_wave_b_next,
    wave_b_design_summary,
    wave_b_history,
    wave_b_readiness,
    wave_b_step_dict,
)
from charmdb.v2.restore_capability import (
    assess_restore_capabilities,
    capability_assessment_dict,
    restore_capability_history,
)
from charmdb.v2.saturation import (
    PHASE1_MANIFEST,
    create_phase1_plan,
    phase1_analysis,
    phase1_history,
    run_phase1_next,
    saturation_step_dict,
)
from charmdb.v2.saturation_phase2 import (
    PHASE2_MANIFEST,
    create_phase2_plan,
    phase2_analysis,
    phase2_history,
    phase2_readiness,
    phase2_step_dict,
    run_phase2_next,
)
from charmdb.v2.screening import (
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
from charmdb.v2.screening_recovery import (
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
from charmdb.v2.secondary_reporting import export_secondary_report
from charmdb.v2.temporal_stability import (
    TEMPORAL_STABILITY_MANIFEST,
    temporal_stability_design_summary,
)
from charmdb.v2.warmup_duration import (
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
from charmdb.v2.window_analysis import analyze_measurement_marker
from charmdb.worker import (
    campaign_status,
    control_campaign,
    create_baseline_benchmark_trial,
    create_campaign,
    create_health_trial,
    create_index_lifecycle_trial,
    create_tuned_benchmark_trial,
    create_v2_baseline_benchmark_trial,
    run_once,
    run_worker_service,
    service_result_json,
    trial_history,
)
from charmdb.worker import (
    result_json as worker_result_json,
)
from charmdb.workload import benchmark_default, seed_pgbench

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


@app.command("v2-bootstrap-preflight")
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


@app.command("v2-candidate-baseline-register")
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


@app.command("v2-candidate-baseline-approve")
def v2_candidate_baseline_approve_command(
    baseline_id: uuid.UUID,
    reason: str,
) -> None:
    baseline = approve_candidate_baseline(get_settings(), baseline_id, reason)
    typer.echo(json.dumps(asdict(baseline), indent=2, default=str))


@app.command("v2-candidate-trial-create")
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


@app.command("v2-candidate-restore-history")
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


@app.command("v2-candidate-restore-ensure")
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


@app.command("v2-restore-capabilities")
def v2_restore_capabilities_command() -> None:
    typer.echo(
        json.dumps(
            capability_assessment_dict(assess_restore_capabilities(get_settings())),
            indent=2,
            default=str,
        )
    )


@app.command("v2-physical-archive-create")
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


@app.command("v2-restore-capability-history")
def v2_restore_capability_history_command(
    limit: int = typer.Option(100, min=1, max=1000),
) -> None:
    typer.echo(json.dumps(restore_capability_history(get_settings(), limit), indent=2, default=str))


@app.command("v2-physical-archive-history")
def v2_physical_archive_history_command(
    limit: int = typer.Option(100, min=1, max=1000),
) -> None:
    typer.echo(json.dumps(physical_archive_history(get_settings(), limit), indent=2, default=str))


@app.command("v2-saturation-phase1-create")
def v2_saturation_phase1_create_command(
    manifest: Path = PHASE1_MANIFEST,
) -> None:
    campaign_id = create_phase1_plan(get_settings(), manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("v2-saturation-phase1-run-next")
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


@app.command("v2-saturation-phase1-history")
def v2_saturation_phase1_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(phase1_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-saturation-phase1-analysis")
def v2_saturation_phase1_analysis_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(phase1_analysis(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-saturation-phase2-create")
def v2_saturation_phase2_create_command(
    preflight_id: uuid.UUID,
    manifest: Path = PHASE2_MANIFEST,
) -> None:
    campaign_id = create_phase2_plan(get_settings(), preflight_id, manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("v2-saturation-phase2-validate")
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


@app.command("v2-saturation-phase2-run-next")
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


@app.command("v2-saturation-phase2-history")
def v2_saturation_phase2_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(phase2_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-saturation-phase2-analysis")
def v2_saturation_phase2_analysis_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(phase2_analysis(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-window-analyze")
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


@app.command("v2-warmup-duration-validate")
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


@app.command("v2-warmup-duration-create")
def v2_warmup_duration_create_command(
    preflight_id: uuid.UUID,
    manifest: Path = PILOT_MANIFEST,
) -> None:
    campaign_id = create_pilot_plan(get_settings(), preflight_id, manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("v2-warmup-duration-run-next")
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


@app.command("v2-warmup-duration-history")
def v2_warmup_duration_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(pilot_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-warmup-analyze")
def v2_warmup_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(analyze_warmup(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-warmup-freeze")
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


@app.command("v2-duration-analyze")
def v2_duration_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(analyze_duration(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-default-reference-validate")
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


@app.command("v2-default-reference-create")
def v2_default_reference_create_command(
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = DEFAULT_REFERENCE_MANIFEST,
) -> None:
    campaign_id = create_default_reference_plan(get_settings(), preflight_id, manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("v2-default-reference-run-next")
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


@app.command("v2-default-reference-history")
def v2_default_reference_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(default_reference_history(get_settings(), campaign_id), indent=2, default=str)
    )


@app.command("v2-default-reference-analyze")
def v2_default_reference_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(analyze_default_reference(get_settings(), campaign_id), indent=2, default=str)
    )


@app.command("v2-default-reference-export")
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


@app.command("v2-screening-design-validate")
def v2_screening_design_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = SCREENING_MANIFEST,
) -> None:
    typer.echo(json.dumps(screening_design_summary(manifest), indent=2))


@app.command("v2-screening-design-export")
def v2_screening_design_export_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = SCREENING_MANIFEST,
    output: Annotated[Path | None, typer.Option(file_okay=True, dir_okay=False)] = None,
) -> None:
    typer.echo(json.dumps(export_screening_design(manifest, output), indent=2))


@app.command("v2-screening-validate")
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


@app.command("v2-screening-create")
def v2_screening_create_command(
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = SCREENING_MANIFEST,
) -> None:
    campaign_id = create_screening_plan(get_settings(), preflight_id, manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("v2-screening-run-next")
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


@app.command("v2-screening-history")
def v2_screening_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(screening_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-screening-analyze")
def v2_screening_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(analyze_screening(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-screening-export")
def v2_screening_export_command(
    campaign_id: uuid.UUID,
    output: Annotated[Path | None, typer.Option(file_okay=True, dir_okay=False)] = None,
) -> None:
    typer.echo(json.dumps(export_screening_analysis(get_settings(), campaign_id, output), indent=2))


@app.command("v2-screening-recovery-design-validate")
def v2_screening_recovery_design_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = RECOVERY_MANIFEST,
) -> None:
    typer.echo(json.dumps(recovery_design_summary(manifest), indent=2))


@app.command("v2-screening-recovery-remediation-validate")
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


@app.command("v2-screening-recovery-remediation-create")
def v2_screening_recovery_remediation_create_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = REMEDIATION_MANIFEST,
) -> None:
    remediation_id = create_target_database_remediation(get_settings(), manifest)
    typer.echo(json.dumps({"remediation_id": str(remediation_id), "status": "PLANNED"}, indent=2))


@app.command("v2-screening-recovery-remediation-run")
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


@app.command("v2-screening-recovery-remediation-history")
def v2_screening_recovery_remediation_history_command(remediation_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(
            target_database_remediation_history(get_settings(), remediation_id),
            indent=2,
            default=str,
        )
    )


@app.command("v2-screening-recovery-restore-validate")
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


@app.command("v2-screening-recovery-restore-create")
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


@app.command("v2-screening-recovery-restore-run-next")
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


@app.command("v2-screening-recovery-restore-history")
def v2_screening_recovery_restore_history_command(validation_block_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(
            restore_stability_history(get_settings(), validation_block_id),
            indent=2,
            default=str,
        )
    )


@app.command("v2-screening-recovery-validate")
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


@app.command("v2-screening-recovery-create")
def v2_screening_recovery_create_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = RECOVERY_MANIFEST,
) -> None:
    campaign_id = create_screening_recovery_plan(get_settings(), manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("v2-screening-recovery-run-next")
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


@app.command("v2-screening-recovery-history")
def v2_screening_recovery_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(screening_recovery_history(get_settings(), campaign_id), indent=2, default=str)
    )


@app.command("v2-screening-recovery-analyze")
def v2_screening_recovery_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(analyze_screening_recovery(get_settings(), campaign_id), indent=2, default=str)
    )


@app.command("v2-screening-recovery-export")
def v2_screening_recovery_export_command(
    campaign_id: uuid.UUID,
    output: Annotated[Path | None, typer.Option(file_okay=True, dir_okay=False)] = None,
) -> None:
    typer.echo(
        json.dumps(
            export_screening_recovery_analysis(get_settings(), campaign_id, output), indent=2
        )
    )


@app.command("v2-temporal-stability-design-validate")
def v2_temporal_stability_design_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = TEMPORAL_STABILITY_MANIFEST,
) -> None:
    typer.echo(json.dumps(temporal_stability_design_summary(manifest), indent=2))


@app.command("v2-primary-design-validate")
def v2_primary_design_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_MANIFEST,
) -> None:
    typer.echo(json.dumps(primary_design_summary(manifest), indent=2))


@app.command("v2-primary-design-export")
def v2_primary_design_export_command(
    output: Path | None = None,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_MANIFEST,
) -> None:
    typer.echo(json.dumps(export_primary_design(manifest, output), indent=2))


@app.command("v2-primary-validate")
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


@app.command("v2-primary-restore-soak-validate")
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


@app.command("v2-primary-restore-soak-create")
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


@app.command("v2-primary-restore-soak-run")
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


@app.command("v2-primary-restore-soak-history")
def v2_primary_restore_soak_history_command(restore_soak_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(
            primary_restore_soak_history(get_settings(), restore_soak_id),
            indent=2,
            default=str,
        )
    )


@app.command("v2-primary-create")
def v2_primary_create_command(
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_MANIFEST,
) -> None:
    campaign_id = create_primary_plan(get_settings(), preflight_id, manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id)}, indent=2))


@app.command("v2-primary-run-next")
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


@app.command("v2-primary-run")
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


@app.command("v2-primary-history")
def v2_primary_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(primary_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-primary-analyze")
def v2_primary_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(analyze_primary(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-primary-export")
def v2_primary_export_command(campaign_id: uuid.UUID, output: Path | None = None) -> None:
    typer.echo(json.dumps(export_primary_analysis(get_settings(), campaign_id, output), indent=2))


@app.command("v2-primary-report")
def v2_primary_report_command(campaign_id: uuid.UUID, output: Path | None = None) -> None:
    """Render the frozen Wave A tables and figures from a terminal analysis."""
    typer.echo(json.dumps(export_primary_report(get_settings(), campaign_id, output), indent=2))


@app.command("v2-primary-report-secondary")
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


@app.command("v2-primary-wave-b-design-validate")
def v2_primary_wave_b_design_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_WAVE_B_MANIFEST,
) -> None:
    """Validate the frozen, result-blind two-seed Wave B design."""
    typer.echo(json.dumps(wave_b_design_summary(manifest), indent=2))


@app.command("v2-primary-wave-b-design-export")
def v2_primary_wave_b_design_export_command(
    output: Path | None = None,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_WAVE_B_MANIFEST,
) -> None:
    typer.echo(json.dumps(export_wave_b_design(manifest, output), indent=2))


@app.command("v2-primary-wave-b-validate")
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


@app.command("v2-primary-wave-b-create")
def v2_primary_wave_b_create_command(
    preflight_id: uuid.UUID = FROZEN_PREFLIGHT_ID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = PRIMARY_WAVE_B_MANIFEST,
) -> None:
    """Create exactly one Wave B campaign without running an observation."""
    campaign_id = create_wave_b_plan(get_settings(), preflight_id, manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "observations_run": 0}, indent=2))


@app.command("v2-primary-wave-b-run-next")
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


@app.command("v2-primary-wave-b-run")
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


@app.command("v2-primary-wave-b-history")
def v2_primary_wave_b_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(wave_b_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-primary-wave-b-analyze")
def v2_primary_wave_b_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(analyze_wave_b(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-primary-wave-b-export")
def v2_primary_wave_b_export_command(campaign_id: uuid.UUID, output: Path | None = None) -> None:
    typer.echo(json.dumps(export_wave_b_analysis(get_settings(), campaign_id, output), indent=2))


@app.command("v2-primary-wave-b-report")
def v2_primary_wave_b_report_command(campaign_id: uuid.UUID, output: Path | None = None) -> None:
    typer.echo(json.dumps(export_wave_b_report(get_settings(), campaign_id, output), indent=2))


@app.command("v2-primary-final-analyze")
def v2_primary_final_analyze_command(wave_b_campaign_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(
            analyze_final_five_seed(get_settings(), wave_b_campaign_id),
            indent=2,
            default=str,
        )
    )


@app.command("v2-primary-final-export")
def v2_primary_final_export_command(
    wave_b_campaign_id: uuid.UUID, output: Path | None = None
) -> None:
    typer.echo(
        json.dumps(
            export_final_five_seed_analysis(get_settings(), wave_b_campaign_id, output),
            indent=2,
        )
    )


@app.command("v2-primary-final-report")
def v2_primary_final_report_command(
    wave_b_campaign_id: uuid.UUID, output: Path | None = None
) -> None:
    typer.echo(
        json.dumps(
            export_final_five_seed_report(get_settings(), wave_b_campaign_id, output),
            indent=2,
        )
    )


@app.command("v2-multifidelity-phase-a")
def v2_multifidelity_phase_a_command(
    campaign_id: uuid.UUID = PRIMARY_CAMPAIGN_ID,
    output: Path | None = None,
) -> None:
    """Execute the D052 retrospective analysis without running a benchmark."""
    typer.echo(json.dumps(execute_phase_a(get_settings(), campaign_id, output), indent=2))


@app.command("v2-f4-validate")
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


@app.command("v2-f4-create")
def v2_f4_create_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = F4_MANIFEST,
) -> None:
    """Create the immutable 20-slot F4 campaign without starting it."""
    campaign_id = create_f4_plan(get_settings(), manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("v2-f4-run-next")
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


@app.command("v2-f4-run")
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


@app.command("v2-f4-history")
def v2_f4_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(f4_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-f4-analyze")
def v2_f4_analyze_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(analyze_f4(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-f4-export")
def v2_f4_export_command(campaign_id: uuid.UUID, output: Path | None = None) -> None:
    typer.echo(
        json.dumps(export_f4_analysis(get_settings(), campaign_id, output), indent=2, default=str)
    )


@app.command("v2-f4-report")
def v2_f4_report_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(render_f4_report(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-multifidelity-phase-b-validate")
def v2_multifidelity_phase_b_validate_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = MULTIFIDELITY_MANIFEST,
) -> None:
    result = phase_b_readiness(get_settings(), manifest)
    typer.echo(json.dumps(result, indent=2, default=str))
    if not result["ready"]:
        raise typer.Exit(code=1)


@app.command("v2-multifidelity-phase-b-create")
def v2_multifidelity_phase_b_create_command(
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ] = MULTIFIDELITY_MANIFEST,
) -> None:
    campaign_id = create_phase_b_plan(get_settings(), manifest)
    typer.echo(json.dumps({"campaign_id": str(campaign_id), "status": "CREATED"}, indent=2))


@app.command("v2-multifidelity-phase-b-run-next")
def v2_multifidelity_phase_b_run_next_command(
    campaign_id: uuid.UUID,
    owner: str = "v2-multifidelity-phase-b",
    lease_seconds: int = 600,
) -> None:
    step = run_phase_b_next(get_settings(), campaign_id, owner=owner, lease_seconds=lease_seconds)
    typer.echo(json.dumps(phase_b_step_dict(step), indent=2, default=str))


@app.command("v2-multifidelity-phase-b-run")
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


@app.command("v2-multifidelity-phase-b-history")
def v2_multifidelity_phase_b_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(phase_b_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-multifidelity-phase-b-analyze")
def v2_multifidelity_phase_b_analyze_command(campaign_id: uuid.UUID) -> None:
    """Authenticate and persist the terminal Phase B result."""
    typer.echo(json.dumps(analyze_phase_b(get_settings(), campaign_id), indent=2, default=str))


@app.command("v2-multifidelity-phase-b-export")
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


@app.command("v2-multifidelity-phase-b-fixture-status")
def v2_multifidelity_phase_b_fixture_status_command(campaign_id: uuid.UUID) -> None:
    """Verify whether the failed restore is solely the known index-test fixture."""
    typer.echo(
        json.dumps(
            phase_b_fixture_remediation_status(get_settings(), campaign_id),
            indent=2,
            default=str,
        )
    )


@app.command("v2-multifidelity-phase-b-remediate-fixture")
def v2_multifidelity_phase_b_remediate_fixture_command(campaign_id: uuid.UUID) -> None:
    """Remove only the exactly recognized test fixture and reconcile the failed attempt."""
    typer.echo(
        json.dumps(
            remediate_phase_b_test_fixture(get_settings(), campaign_id),
            indent=2,
            default=str,
        )
    )


@app.command("v2-multifidelity-phase-b-validate-remediation")
def v2_multifidelity_phase_b_validate_remediation_command(campaign_id: uuid.UUID) -> None:
    """Verify baseline fingerprints after the guarded test-fixture cleanup."""
    typer.echo(
        json.dumps(
            validate_phase_b_fixture_remediation(get_settings(), campaign_id),
            indent=2,
            default=str,
        )
    )


@app.command("v2-primary-analysis-rehearsal")
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


@app.command("v2-evidence-manifest")
def v2_evidence_manifest_command(root: Path | None = None, output: Path | None = None) -> None:
    artifact_root = get_settings().artifact_dir
    selected_root = root or artifact_root
    typer.echo(
        json.dumps(
            build_evidence_manifest(selected_root, output, artifact_root=artifact_root),
            indent=2,
        )
    )


@app.command("v2-evidence-ledgers-export")
def v2_evidence_ledgers_export_command(output: Path | None = None) -> None:
    typer.echo(
        json.dumps(export_v2_evidence_ledgers(get_settings(), output), indent=2, default=str)
    )


@app.command("knobs-discover")
def knobs_discover() -> None:
    typer.echo(json.dumps(discover_knobs(get_settings()), indent=2, default=str))


@app.command("knobs-apply")
def knobs_apply(knob: str, value: str) -> None:
    result = apply_configuration(get_settings(), {knob: value})
    typer.echo(result_json(result))


@app.command("knobs-rollback")
def knobs_rollback(application_id: uuid.UUID, reason: str = "manual rollback") -> None:
    restored = rollback_configuration(get_settings(), application_id, reason)
    typer.echo(json.dumps({"application_id": str(application_id), "restored": restored}, indent=2))


@app.command("benchmark-default")
def benchmark_default_command() -> None:
    trial_id, path, result = benchmark_default(get_settings())
    typer.echo(
        json.dumps({"trial_id": str(trial_id), "artifact": str(path), **result.__dict__}, indent=2)
    )


@app.command("index-seed")
def index_seed(rows: int = typer.Option(300_000, min=10_000, max=1_000_000)) -> None:
    seed_index_fixture(get_settings(), rows)
    typer.echo(json.dumps({"seeded": True, "rows_requested": rows}))


@app.command("index-experiment")
def index_experiment(customer_id: int = typer.Option(4242, min=0, max=9999)) -> None:
    typer.echo(index_result_json(run_index_experiment(get_settings(), customer_id)))


@app.command("drift-experiment")
def drift_experiment(
    baseline_windows: int = typer.Option(12, min=8, max=100),
    drift_windows: int = typer.Option(12, min=4, max=100),
    recurring_windows: int = typer.Option(8, min=2, max=100),
    window_seconds: int = typer.Option(1, min=1, max=60),
    seed: int = typer.Option(20260713),
) -> None:
    result = run_drift_experiment(
        get_settings(),
        baseline_windows,
        drift_windows,
        recurring_windows,
        window_seconds,
        seed,
    )
    typer.echo(drift_result_json(result))


@app.command("fidelity-pilot")
def fidelity_pilot(
    candidates: int = typer.Option(4, min=4, max=32),
    f2_seconds: int = typer.Option(5, min=3, max=60),
    f3_seconds: int = typer.Option(30, min=10, max=600),
    f4_replicates: int = typer.Option(2, min=2, max=10),
    seed: int = typer.Option(20260715),
) -> None:
    result = run_fidelity_pilot(
        get_settings(), candidates, f2_seconds, f3_seconds, f4_replicates, seed
    )
    typer.echo(fidelity_result_json(result))


@app.command("calibration-pilot")
def calibration_pilot(
    candidates: int = typer.Option(16, min=12, max=64),
    f2_seconds: int = typer.Option(5, min=3, max=60),
    f3_seconds: int = typer.Option(30, min=10, max=600),
    seed: int = typer.Option(20260716),
) -> None:
    result = run_calibration_pilot(get_settings(), candidates, f2_seconds, f3_seconds, seed)
    typer.echo(calibration_result_json(result))


@app.command("gate-status")
def gate_status_command() -> None:
    typer.echo(json.dumps(optimizer_gate_status(get_settings()), indent=2, default=str))


@app.command("experiments-register")
def experiments_register_command() -> None:
    typer.echo(json.dumps(register_experiment_matrix(get_settings()), indent=2))


@app.command("experiments-status")
def experiments_status_command() -> None:
    typer.echo(json.dumps(experiment_registry_status(get_settings()), indent=2, default=str))


@app.command("experiment-gates")
def experiment_gates_command() -> None:
    typer.echo(
        json.dumps(
            [asdict(item) for item in prerequisite_gate_status(get_settings())],
            indent=2,
            default=str,
        )
    )


@app.command("experiment-arm-prepare")
def experiment_arm_prepare_command(
    arm_id: uuid.UUID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ],
    actor: str = "cli",
) -> None:
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise typer.BadParameter("manifest must contain a JSON object")
    typer.echo(
        json.dumps(
            asdict(prepare_experiment_arm(get_settings(), arm_id, payload, actor)),
            indent=2,
            default=str,
        )
    )


@app.command("experiment-manifest-preflight")
def experiment_manifest_preflight_command(
    warmup_seconds: int = typer.Option(120, min=0, max=3600),
    measurement_seconds: int = typer.Option(600, min=1, max=7200),
    concurrency: int = typer.Option(4, min=1, max=128),
) -> None:
    typer.echo(
        json.dumps(
            asdict(
                run_manifest_preflight(
                    get_settings(), warmup_seconds, measurement_seconds, concurrency
                )
            ),
            indent=2,
            default=str,
        )
    )


@app.command("experiment-dataset-restore-validate")
def experiment_dataset_restore_validate_command(preflight_id: uuid.UUID) -> None:
    typer.echo(
        json.dumps(
            asdict(validate_dataset_restore(get_settings(), preflight_id)),
            indent=2,
            default=str,
        )
    )


@app.command("experiment-f3-reference-run")
def experiment_f3_reference_run_command(
    preflight_id: uuid.UUID,
    repetitions: int = typer.Option(5, min=5, max=20),
    seed: int = 20260730,
) -> None:
    typer.echo(
        json.dumps(
            asdict(run_f3_reference_block(get_settings(), preflight_id, repetitions, seed)),
            indent=2,
            default=str,
        )
    )


@app.command("experiment-search-manifest-generate")
def experiment_search_manifest_generate_command(
    preflight_id: uuid.UUID,
    output: Path = Path("config/experiment-manifest.search.json"),
) -> None:
    typer.echo(
        json.dumps(
            asdict(generate_search_manifest(get_settings(), preflight_id, output)),
            indent=2,
            default=str,
        )
    )


@app.command("experiment-manifest-draft")
def experiment_manifest_draft_command(
    preflight_id: uuid.UUID,
    arm_id: uuid.UUID,
    output: Annotated[Path | None, typer.Option(file_okay=True, dir_okay=False)] = None,
) -> None:
    typer.echo(
        json.dumps(
            manifest_result_json(
                draft_search_manifest(get_settings(), preflight_id, arm_id, output)
            ),
            indent=2,
        )
    )


@app.command("experiment-manifest-review")
def experiment_manifest_review_command(
    preflight_id: uuid.UUID,
    arm_id: uuid.UUID,
    manifest: Annotated[
        Path, typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True)
    ],
) -> None:
    typer.echo(
        json.dumps(
            manifest_result_json(
                review_search_manifest(get_settings(), preflight_id, arm_id, manifest)
            ),
            indent=2,
        )
    )


@app.command("experiment-arm-status")
def experiment_arm_status_command(arm_id: uuid.UUID) -> None:
    typer.echo(json.dumps(experiment_arm_history(get_settings(), arm_id), indent=2, default=str))


@app.command("experiment-arm-start")
def experiment_arm_start_command(arm_id: uuid.UUID, actor: str = "cli") -> None:
    typer.echo(
        json.dumps(
            progress_json(start_or_resume_search_arm(get_settings(), arm_id, actor)), indent=2
        )
    )


@app.command("experiment-search-arm-run")
def experiment_search_arm_run_command(
    arm_id: uuid.UUID,
    actor: str = "search-orchestrator",
    owner: str = "search-arm-worker",
) -> None:
    typer.echo(
        json.dumps(progress_json(run_search_arm(get_settings(), arm_id, actor, owner)), indent=2)
    )


@app.command("experiment-arm-budget-record")
def experiment_arm_budget_record_command(
    arm_id: uuid.UUID,
    trial_id: uuid.UUID,
    phase: str = "static",
    actor: str = "cli",
) -> None:
    typer.echo(
        json.dumps(
            record_experiment_trial_budget(get_settings(), arm_id, trial_id, phase, actor),
            indent=2,
            default=str,
        )
    )


@app.command("experiment-arm-finalize")
def experiment_arm_finalize_command(arm_id: uuid.UUID, actor: str = "cli") -> None:
    typer.echo(json.dumps(finalize_experiment_arm(get_settings(), arm_id, actor), indent=2))


@app.command("analysis-readiness")
def analysis_readiness_command() -> None:
    typer.echo(json.dumps(asdict(analysis_readiness(get_settings())), indent=2, default=str))


@app.command("analysis-generate")
def analysis_generate_command() -> None:
    result = generate_analysis_readiness(get_settings())
    typer.echo(
        json.dumps(
            {
                "analysis_report_id": str(result.analysis_report_id),
                "output_path": str(result.output_path),
                "sha256": result.sha256,
                "status": result.readiness.status,
            },
            indent=2,
        )
    )


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


@app.command("coordination-history")
def coordination_history_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(coordination_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("trial-health-create")
def trial_health_create(
    campaign_id: uuid.UUID,
    idempotency_key: str,
    seed: int = 20260713,
    max_attempts: int = typer.Option(3, min=1, max=20),
) -> None:
    trial_id = create_health_trial(get_settings(), campaign_id, seed, idempotency_key, max_attempts)
    typer.echo(json.dumps({"trial_id": str(trial_id), "state": "CREATED"}, indent=2))


@app.command("trial-baseline-create")
def trial_baseline_create(
    campaign_id: uuid.UUID,
    idempotency_key: str,
    seed: int = 20260713,
    warmup_seconds: int = typer.Option(2, min=0, max=600),
    duration_seconds: int = typer.Option(5, min=1, max=3600),
    concurrency: int = typer.Option(4, min=1, max=128),
    fidelity: int = typer.Option(3, min=2, max=4),
    p99_slo_ms: float = typer.Option(20.0, min=0.001),
    max_attempts: int = typer.Option(3, min=1, max=20),
) -> None:
    trial_id = create_baseline_benchmark_trial(
        get_settings(),
        campaign_id,
        seed,
        idempotency_key,
        warmup_seconds,
        duration_seconds,
        concurrency,
        fidelity,
        p99_slo_ms,
        max_attempts,
    )
    typer.echo(json.dumps({"trial_id": str(trial_id), "state": "CREATED"}, indent=2))


@app.command("trial-tuned-create")
def trial_tuned_create(
    campaign_id: uuid.UUID,
    knob: str,
    value: str,
    idempotency_key: str,
    seed: int = 20260713,
    warmup_seconds: int = typer.Option(2, min=0, max=600),
    duration_seconds: int = typer.Option(5, min=1, max=3600),
    concurrency: int = typer.Option(4, min=1, max=128),
    p99_slo_ms: float = typer.Option(20.0, min=0.001),
    max_attempts: int = typer.Option(3, min=1, max=20),
) -> None:
    trial_id = create_tuned_benchmark_trial(
        get_settings(),
        campaign_id,
        {knob: value},
        seed,
        idempotency_key,
        warmup_seconds,
        duration_seconds,
        concurrency,
        p99_slo_ms,
        max_attempts,
    )
    typer.echo(json.dumps({"trial_id": str(trial_id), "state": "CREATED"}, indent=2))


@app.command("trial-index-create")
def trial_index_create(
    campaign_id: uuid.UUID,
    table_name: str,
    key_columns: str,
    idempotency_key: str,
    schema_name: str = "public",
    include_columns: str = "",
    seed: int = 20260713,
    max_attempts: int = typer.Option(3, min=1, max=20),
) -> None:
    keys = tuple(value.strip() for value in key_columns.split(",") if value.strip())
    includes = tuple(value.strip() for value in include_columns.split(",") if value.strip())
    trial_id = create_index_lifecycle_trial(
        get_settings(),
        campaign_id,
        schema_name,
        table_name,
        keys,
        includes,
        seed,
        idempotency_key,
        max_attempts,
    )
    typer.echo(json.dumps({"trial_id": str(trial_id), "state": "CREATED"}, indent=2))


@app.command("trial-history")
def trial_history_command(trial_id: uuid.UUID) -> None:
    typer.echo(json.dumps(trial_history(get_settings(), trial_id), indent=2, default=str))


@app.command("drift-history")
def drift_history_command(
    limit: int = typer.Option(100, min=1, max=1000),
    context_id: uuid.UUID | None = None,
) -> None:
    typer.echo(json.dumps(drift_history(get_settings(), limit, context_id), indent=2, default=str))


@app.command("index-history")
def index_history_command(
    candidate_id: uuid.UUID | None = None,
    limit: int = typer.Option(100, min=1, max=1000),
) -> None:
    payload: object
    if candidate_id is None:
        payload = index_history(get_settings(), limit)
    else:
        payload = index_candidate_history(get_settings(), candidate_id)
    typer.echo(json.dumps(payload, indent=2, default=str))


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


def _not_implemented(command: str) -> None:
    raise typer.BadParameter(f"{command} is not implemented before its ordered research slice")


@app.command("tune-single")
def tune_single(
    sobol_trials: int = typer.Option(4, min=4, max=64),
    bo_trials: int = typer.Option(1, min=1, max=64),
    seed: int = typer.Option(20260712),
) -> None:
    campaign_id, observations = run_single_objective_campaign(
        get_settings(), sobol_trials, bo_trials, seed
    )
    typer.echo(observations_json(campaign_id, observations))


@app.command("tune-multi")
def tune_multi(
    sobol_trials: int = typer.Option(4, min=4, max=64),
    bo_trials: int = typer.Option(1, min=1, max=64),
    f4_replicates: int = typer.Option(2, min=2, max=10),
    seed: int = typer.Option(20260712),
    context_label: str = "development-pgbench",
    acquisition: str = typer.Option("qLogNEHVI"),
    pool_size: int = typer.Option(1024, min=16, max=16384),
    sample_count: int = typer.Option(128, min=16, max=2048),
) -> None:
    if acquisition not in {"qLogNEHVI", "qLogNParEGO"}:
        raise typer.BadParameter("acquisition must be qLogNEHVI or qLogNParEGO")
    result = run_multiobjective_campaign(
        get_settings(),
        sobol_trials,
        bo_trials,
        seed,
        context_label,
        f4_replicates,
        acquisition,  # type: ignore[arg-type]
        pool_size,
        sample_count,
    )
    typer.echo(json.dumps(multiobjective_result_dict(result), indent=2))


@app.command("pareto-front")
def pareto_front_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(pareto_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("champion")
def champion_command(campaign_id: uuid.UUID) -> None:
    typer.echo(json.dumps(champion_history(get_settings(), campaign_id), indent=2, default=str))


@app.command("recommendation-verify")
def recommendation_verify_command(recommendation_id: uuid.UUID) -> None:
    typer.echo(json.dumps(verify_recommendation(get_settings(), recommendation_id), indent=2))


@app.command("tune-coordinated")
def tune_coordinated() -> None:
    _not_implemented("tune-coordinated")


@app.command()
def evaluate() -> None:
    _not_implemented("evaluate")


@app.command("evaluate-drift")
def evaluate_drift() -> None:
    typer.echo(drift_result_json(run_drift_experiment(get_settings())))


@app.command("evaluate-ablation")
def evaluate_ablation() -> None:
    _not_implemented("evaluate-ablation")


@app.command()
def report() -> None:
    result = generate_report(get_settings())
    typer.echo(
        json.dumps(
            {
                "output_dir": str(result.output_dir),
                "markdown": str(result.markdown_path),
                "json": str(result.json_path),
                "csv": str(result.csv_path),
                "manifest": str(result.manifest_path),
                "chart": str(result.chart_path) if result.chart_path else None,
                "campaigns": result.campaigns,
                "trials": result.trials,
            },
            indent=2,
        )
    )


@app.command("soak-test")
def soak_test(duration: str = typer.Option(...)) -> None:
    typer.echo(json.dumps(soak_result_dict(run_soak_test(get_settings(), duration)), indent=2))


if __name__ == "__main__":
    app()
