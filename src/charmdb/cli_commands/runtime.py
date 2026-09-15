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

from charmdb.bootstrap import (
    bootstrap_report_dict,
    run_bootstrap_preflight,
    write_bootstrap_report,
)
from charmdb.cli_commands import app
from charmdb.config import get_settings
from charmdb.controller import (
    discover_knobs,
    rollback_configuration,
)
from charmdb.db import apply_migrations, connect
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
    create_baseline_benchmark_trial,
    create_campaign,
    create_health_trial,
    run_once,
    run_worker_service,
    service_result_json,
    trial_history,
)
from charmdb.worker import (
    result_json as worker_result_json,
)
from charmdb.workload import seed_pgbench


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
    trial_id = create_baseline_benchmark_trial(
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
