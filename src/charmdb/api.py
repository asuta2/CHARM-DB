from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from charmdb import __version__
from charmdb.analysis import analysis_readiness
from charmdb.config import get_settings
from charmdb.controller import rollback_configuration
from charmdb.coordination import coordination_history
from charmdb.db import connect
from charmdb.experiment_execution import (
    experiment_arm_history,
    finalize_experiment_arm,
    prepare_experiment_arm,
    prerequisite_gate_status,
    record_experiment_trial_budget,
)
from charmdb.experiments import experiment_registry_status
from charmdb.gating import optimizer_gate_status
from charmdb.multiobjective import champion_history, pareto_history, verify_recommendation
from charmdb.observability import render_prometheus_metrics
from charmdb.operations import drift_history, index_candidate_history, index_history
from charmdb.reporting import generate_report
from charmdb.search_execution import progress_json, start_or_resume_search_arm
from charmdb.worker import (
    campaign_status,
    campaign_trials,
    control_campaign,
    create_baseline_benchmark_trial,
    create_campaign,
    create_health_trial,
    create_index_lifecycle_trial,
    create_tuned_benchmark_trial,
    trial_history,
)

app = FastAPI(title="CHARM-DB", version=__version__)


class CampaignCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    mode: str = Field(default="MONITOR", min_length=1, max_length=80)
    objective_definition: dict[str, Any] = Field(default_factory=dict)
    constraint_definition: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    failure_limit: int = Field(default=5, ge=1, le=100)
    actor: str = Field(default="api", min_length=1, max_length=200)


class CampaignActionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    actor: str = Field(default="api", min_length=1, max_length=200)


class RollbackRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class HealthTrialRequest(BaseModel):
    seed: int = 20260713
    idempotency_key: str = Field(min_length=1, max_length=200)
    max_attempts: int = Field(default=3, ge=1, le=20)


class BaselineBenchmarkTrialRequest(BaseModel):
    seed: int = 20260713
    idempotency_key: str = Field(min_length=1, max_length=200)
    warmup_seconds: int = Field(default=2, ge=0, le=600)
    duration_seconds: int = Field(default=5, ge=1, le=3600)
    concurrency: int = Field(default=4, ge=1, le=128)
    fidelity: int = Field(default=3, ge=2, le=4)
    p99_slo_ms: float = Field(default=20.0, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=20)


class TunedBenchmarkTrialRequest(BaseModel):
    configuration: dict[str, str] = Field(min_length=1, max_length=3)
    seed: int = 20260713
    idempotency_key: str = Field(min_length=1, max_length=200)
    warmup_seconds: int = Field(default=2, ge=0, le=600)
    duration_seconds: int = Field(default=5, ge=1, le=3600)
    concurrency: int = Field(default=4, ge=1, le=128)
    p99_slo_ms: float = Field(default=20.0, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=20)


class IndexLifecycleTrialRequest(BaseModel):
    schema_name: str = Field(default="public", min_length=1, max_length=63)
    table_name: str = Field(min_length=1, max_length=63)
    key_columns: list[str] = Field(min_length=1, max_length=3)
    include_columns: list[str] = Field(default_factory=list, max_length=2)
    seed: int = 20260713
    idempotency_key: str = Field(min_length=1, max_length=200)
    max_attempts: int = Field(default=3, ge=1, le=20)


class ExperimentArmPrepareRequest(BaseModel):
    execution_manifest: dict[str, Any]
    actor: str = Field(default="api", min_length=1, max_length=200)


class ExperimentBudgetRequest(BaseModel):
    trial_id: uuid.UUID
    phase: str = Field(default="static", min_length=1, max_length=80)
    actor: str = Field(default="api", min_length=1, max_length=200)


class ExperimentFinalizeRequest(BaseModel):
    actor: str = Field(default="api", min_length=1, max_length=200)


@app.exception_handler(ValueError)
def value_error_handler(_request: Request, error: ValueError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": {"code": "INVALID_REQUEST", "message": str(error)}},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/ready")
def ready() -> dict[str, Any]:
    settings = get_settings()
    results: dict[str, bool] = {}
    for name, dsn in (("control", settings.control_dsn), ("target", settings.target_dsn)):
        try:
            with connect(dsn) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                results[name] = cur.fetchone() is not None
        except Exception:
            results[name] = False
    return {"ready": all(results.values()), "dependencies": results}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return render_prometheus_metrics(get_settings())


@app.get("/optimizer-gates/status")
def optimizer_gate_status_endpoint() -> dict[str, Any]:
    return optimizer_gate_status(get_settings())


@app.get("/experiments")
def experiment_registry_status_endpoint() -> list[dict[str, Any]]:
    return experiment_registry_status(get_settings())


@app.get("/experiments/gates")
def experiment_gate_status_endpoint() -> list[dict[str, Any]]:
    return [
        {
            "gate": item.gate,
            "eligible": item.eligible,
            "reasons": list(item.reasons),
            "evidence": item.evidence,
        }
        for item in prerequisite_gate_status(get_settings())
    ]


@app.get("/experiments/arms/{arm_id}")
def experiment_arm_history_endpoint(arm_id: uuid.UUID) -> dict[str, Any]:
    return experiment_arm_history(get_settings(), arm_id)


@app.post("/experiments/arms/{arm_id}/prepare")
def prepare_experiment_arm_endpoint(
    arm_id: uuid.UUID, request: ExperimentArmPrepareRequest
) -> dict[str, Any]:
    result = prepare_experiment_arm(
        get_settings(), arm_id, request.execution_manifest, request.actor
    )
    return {
        "arm_id": str(result.arm_id),
        "campaign_id": str(result.campaign_id) if result.campaign_id else None,
        "status": result.status,
        "manifest_sha256": result.manifest_sha256,
        "gate": {
            "name": result.gate.gate,
            "eligible": result.gate.eligible,
            "reasons": list(result.gate.reasons),
            "evidence": result.gate.evidence,
        },
    }


@app.post("/experiments/arms/{arm_id}/budget")
def record_experiment_budget_endpoint(
    arm_id: uuid.UUID, request: ExperimentBudgetRequest
) -> dict[str, Any]:
    return record_experiment_trial_budget(
        get_settings(), arm_id, request.trial_id, request.phase, request.actor
    )


@app.post("/experiments/arms/{arm_id}/start")
def start_experiment_arm_endpoint(
    arm_id: uuid.UUID, request: ExperimentFinalizeRequest
) -> dict[str, Any]:
    return progress_json(start_or_resume_search_arm(get_settings(), arm_id, request.actor))


@app.post("/experiments/arms/{arm_id}/finalize")
def finalize_experiment_arm_endpoint(
    arm_id: uuid.UUID, request: ExperimentFinalizeRequest
) -> dict[str, Any]:
    return finalize_experiment_arm(get_settings(), arm_id, request.actor)


@app.get("/analysis/readiness")
def analysis_readiness_endpoint() -> dict[str, Any]:
    readiness = analysis_readiness(get_settings())
    return {
        "status": readiness.status,
        "registered_groups": readiness.registered_groups,
        "registered_arms": readiness.registered_arms,
        "completed_arms": readiness.completed_arms,
        "missing_arms": list(readiness.missing_arms),
    }


@app.post("/campaigns", status_code=201)
def create_campaign_endpoint(request: CampaignCreateRequest) -> dict[str, str]:
    campaign_id = create_campaign(
        get_settings(),
        request.name,
        request.mode,
        request.objective_definition,
        request.constraint_definition,
        request.failure_limit,
        request.settings,
        request.actor,
    )
    return {"campaign_id": str(campaign_id), "status": "CREATED"}


@app.get("/campaigns/{campaign_id}")
def campaign_status_endpoint(campaign_id: uuid.UUID) -> dict[str, Any]:
    return campaign_status(get_settings(), campaign_id)


@app.get("/campaigns/{campaign_id}/trials")
def campaign_trials_endpoint(campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    return campaign_trials(get_settings(), campaign_id)


@app.get("/campaigns/{campaign_id}/coordination")
def coordination_history_endpoint(campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    return coordination_history(get_settings(), campaign_id)


def _campaign_action(
    campaign_id: uuid.UUID, action: str, request: CampaignActionRequest
) -> dict[str, Any]:
    return control_campaign(get_settings(), campaign_id, action, request.reason, request.actor)


@app.post("/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: uuid.UUID, request: CampaignActionRequest) -> dict[str, Any]:
    return _campaign_action(campaign_id, "pause", request)


@app.post("/campaigns/{campaign_id}/resume")
def resume_campaign(campaign_id: uuid.UUID, request: CampaignActionRequest) -> dict[str, Any]:
    return _campaign_action(campaign_id, "resume", request)


@app.post("/campaigns/{campaign_id}/stop")
def stop_campaign(campaign_id: uuid.UUID, request: CampaignActionRequest) -> dict[str, Any]:
    return _campaign_action(campaign_id, "stop", request)


@app.post("/campaigns/{campaign_id}/emergency-stop")
def emergency_stop_campaign(
    campaign_id: uuid.UUID, request: CampaignActionRequest
) -> dict[str, Any]:
    return _campaign_action(campaign_id, "emergency-stop", request)


@app.post("/campaigns/{campaign_id}/trials/health", status_code=201)
def create_health_trial_endpoint(
    campaign_id: uuid.UUID, request: HealthTrialRequest
) -> dict[str, str]:
    trial_id = create_health_trial(
        get_settings(),
        campaign_id,
        request.seed,
        request.idempotency_key,
        request.max_attempts,
    )
    return {"trial_id": str(trial_id), "state": "CREATED"}


@app.post("/campaigns/{campaign_id}/trials/baseline-benchmark", status_code=201)
def create_baseline_benchmark_trial_endpoint(
    campaign_id: uuid.UUID, request: BaselineBenchmarkTrialRequest
) -> dict[str, str]:
    trial_id = create_baseline_benchmark_trial(
        get_settings(),
        campaign_id,
        request.seed,
        request.idempotency_key,
        request.warmup_seconds,
        request.duration_seconds,
        request.concurrency,
        request.fidelity,
        request.p99_slo_ms,
        request.max_attempts,
    )
    return {"trial_id": str(trial_id), "state": "CREATED"}


@app.post("/campaigns/{campaign_id}/trials/tuned-benchmark", status_code=201)
def create_tuned_benchmark_trial_endpoint(
    campaign_id: uuid.UUID, request: TunedBenchmarkTrialRequest
) -> dict[str, str]:
    trial_id = create_tuned_benchmark_trial(
        get_settings(),
        campaign_id,
        request.configuration,
        request.seed,
        request.idempotency_key,
        request.warmup_seconds,
        request.duration_seconds,
        request.concurrency,
        request.p99_slo_ms,
        request.max_attempts,
    )
    return {"trial_id": str(trial_id), "state": "CREATED"}


@app.post("/campaigns/{campaign_id}/trials/index-lifecycle", status_code=201)
def create_index_lifecycle_trial_endpoint(
    campaign_id: uuid.UUID, request: IndexLifecycleTrialRequest
) -> dict[str, str]:
    trial_id = create_index_lifecycle_trial(
        get_settings(),
        campaign_id,
        request.schema_name,
        request.table_name,
        tuple(request.key_columns),
        tuple(request.include_columns),
        request.seed,
        request.idempotency_key,
        request.max_attempts,
    )
    return {"trial_id": str(trial_id), "state": "CREATED"}


@app.get("/trials/{trial_id}")
def trial_history_endpoint(trial_id: uuid.UUID) -> dict[str, Any]:
    return trial_history(get_settings(), trial_id)


@app.post("/configurations/{application_id}/rollback")
def rollback_configuration_endpoint(
    application_id: uuid.UUID, request: RollbackRequest
) -> dict[str, Any]:
    restored = rollback_configuration(get_settings(), application_id, request.reason)
    return {"application_id": str(application_id), "restored": restored}


@app.get("/drift-events")
def drift_history_endpoint(
    limit: int = 100, context_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    return drift_history(get_settings(), limit, context_id)


@app.get("/indexes")
def index_history_endpoint(limit: int = 100) -> list[dict[str, Any]]:
    return index_history(get_settings(), limit)


@app.get("/indexes/{candidate_id}")
def index_candidate_history_endpoint(candidate_id: uuid.UUID) -> dict[str, Any]:
    return index_candidate_history(get_settings(), candidate_id)


@app.get("/campaigns/{campaign_id}/pareto")
def pareto_history_endpoint(campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    return pareto_history(get_settings(), campaign_id)


@app.get("/campaigns/{campaign_id}/champion")
def champion_history_endpoint(campaign_id: uuid.UUID) -> dict[str, Any]:
    return champion_history(get_settings(), campaign_id)


@app.get("/recommendations/{recommendation_id}/verify")
def verify_recommendation_endpoint(recommendation_id: uuid.UUID) -> dict[str, Any]:
    return verify_recommendation(get_settings(), recommendation_id)


@app.post("/reports", status_code=201)
def generate_report_endpoint() -> dict[str, Any]:
    result = generate_report(get_settings())
    return {
        "output_dir": str(result.output_dir),
        "markdown": str(result.markdown_path),
        "json": str(result.json_path),
        "csv": str(result.csv_path),
        "manifest": str(result.manifest_path),
        "chart": str(result.chart_path) if result.chart_path else None,
        "campaigns": result.campaigns,
        "trials": result.trials,
    }
