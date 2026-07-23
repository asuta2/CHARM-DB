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
from charmdb.worker import (
    campaign_status,
    control_campaign,
    create_baseline_benchmark_trial,
    create_campaign,
    create_health_trial,
    create_index_lifecycle_trial,
    create_tuned_benchmark_trial,
    run_once,
    run_worker_service,
    service_result_json,
    trial_history,
)
from charmdb.worker import (
    result_json as worker_result_json,
)
from charmdb.workload import benchmark_default, seed_pgbench

app = typer.Typer(no_args_is_help=True, help="CHARM-DB research controller")


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
