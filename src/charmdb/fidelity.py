from __future__ import annotations

import hashlib
import json
import statistics
import time
import uuid
from dataclasses import dataclass

from psycopg.types.json import Jsonb
from scipy.stats import kendalltau, spearmanr  # type: ignore[import-untyped]

from charmdb.config import Settings
from charmdb.controller import (
    apply_configuration,
    discover_knobs,
    rollback_configuration,
    validate_candidate,
)
from charmdb.db import connect
from charmdb.optimizer import P99_SLO_MS, Candidate, sobol_candidates
from charmdb.workload import WorkloadResult, benchmark_default


@dataclass(frozen=True)
class PromotionDecision:
    promoted: bool
    reason: str


@dataclass(frozen=True)
class EarlyStopDecision:
    stopped: bool
    reason: str


@dataclass(frozen=True)
class FidelityPair:
    candidate: Candidate
    f2: WorkloadResult
    f3: WorkloadResult
    promoted: bool


@dataclass(frozen=True)
class FidelityPilotResult:
    campaign_id: uuid.UUID
    paired_candidates: int
    spearman: float
    kendall: float
    mean_bias_tps: float
    promotion_confusion: dict[str, int]
    early_stopping_enabled: bool
    champion_configuration: dict[str, str]
    f4_throughputs: tuple[float, ...]
    wall_clock_seconds: float


def decide_promotion(
    result: WorkloadResult, baseline_tps: float, minimum_ratio: float = 0.8
) -> PromotionDecision:
    if result.failures > 0:
        return PromotionDecision(False, "F2 failures")
    if result.p99_ms > P99_SLO_MS:
        return PromotionDecision(False, "F2 p99 constraint violated")
    if result.throughput_tps < baseline_tps * minimum_ratio:
        return PromotionDecision(False, "F2 throughput below promotion floor")
    return PromotionDecision(True, "F2 feasible and above promotion floor")


def decide_early_stop(
    observed_tps: float,
    observed_p99_ms: float,
    baseline_tps: float,
    elapsed_seconds: float,
    protected_seconds: float = 5.0,
) -> EarlyStopDecision:
    if elapsed_seconds < protected_seconds:
        return EarlyStopDecision(False, "protected measurement window")
    if observed_p99_ms > P99_SLO_MS * 1.5:
        return EarlyStopDecision(True, "severe sustained p99 violation")
    if observed_tps < baseline_tps * 0.7:
        return EarlyStopDecision(True, "throughput below conservative stop boundary")
    return EarlyStopDecision(False, "no conservative stop boundary crossed")


def early_stopping_gate(paired_candidates: int, spearman: float, false_negatives: int) -> bool:
    return paired_candidates >= 12 and spearman >= 0.5 and false_negatives == 0


def _candidate_key(configuration: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()


def _baseline_tps(settings: Settings) -> float:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT avg((objective_values->>'throughput_tps')::double precision) AS mean_tps
            FROM charm_control.trials WHERE state='COMPLETED' AND fidelity=3
              AND benchmark_profile='development-default' AND active_configuration='{}'::jsonb"""
        )
        row = cur.fetchone()
        if row is None or row["mean_tps"] is None:
            raise RuntimeError("default F3 baseline observations are required")
        return float(row["mean_tps"])


def _planner_cost(settings: Settings) -> float:
    statements = (
        "SELECT abalance FROM pgbench_accounts WHERE aid=500000",
        "UPDATE pgbench_accounts SET abalance=abalance+1 WHERE aid=500000",
    )
    total = 0.0
    with connect(settings.target_dsn) as conn, conn.cursor() as cur:
        for statement in statements:
            cur.execute("EXPLAIN (FORMAT JSON) " + statement)
            plan = cur.fetchone()["QUERY PLAN"][0]["Plan"]  # type: ignore[index]
            total += float(plan["Total Cost"])
    return total


def _persist_fidelity(
    settings: Settings,
    campaign_id: uuid.UUID,
    candidate: Candidate,
    fidelity: int,
    replicate: int,
    wall_clock: float,
    evidence_kind: str,
    result: WorkloadResult | None = None,
    trial_id: uuid.UUID | None = None,
    planner_cost: float | None = None,
    feasible: bool = True,
) -> uuid.UUID:
    observation_id = uuid.uuid4()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.fidelity_observations
            (fidelity_observation_id,campaign_id,candidate_key,configuration,fidelity,replicate,
             benchmark_trial_id,planner_cost,throughput_tps,p95_ms,p99_ms,failures,feasible,
             wall_clock_seconds,evidence_kind)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                observation_id,
                campaign_id,
                _candidate_key(candidate.configuration),
                Jsonb(candidate.configuration),
                fidelity,
                replicate,
                trial_id,
                planner_cost,
                result.throughput_tps if result else None,
                result.p95_ms if result else None,
                result.p99_ms if result else None,
                result.failures if result else None,
                feasible,
                wall_clock,
                evidence_kind,
            ),
        )
        conn.commit()
    return observation_id


def _persist_promotion(
    settings: Settings,
    campaign_id: uuid.UUID,
    candidate: Candidate,
    decision: PromotionDecision,
    f2: WorkloadResult,
    baseline_tps: float,
) -> uuid.UUID:
    promotion_id = uuid.uuid4()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.promotion_decisions
            (promotion_id,campaign_id,candidate_key,from_fidelity,to_fidelity,promoted,
             decision_rule,decision_inputs) VALUES (%s,%s,%s,2,3,%s,%s,%s)""",
            (
                promotion_id,
                campaign_id,
                _candidate_key(candidate.configuration),
                decision.promoted,
                "F2 feasible; p99<=20ms; throughput>=0.8*default",
                Jsonb(
                    {
                        "reason": decision.reason,
                        "f2_tps": f2.throughput_tps,
                        "f2_p99_ms": f2.p99_ms,
                        "f2_failures": f2.failures,
                        "baseline_tps": baseline_tps,
                    }
                ),
            ),
        )
        conn.commit()
    return promotion_id


def _persist_early_stop(
    settings: Settings,
    campaign_id: uuid.UUID,
    candidate: Candidate,
    decision: EarlyStopDecision,
    f2: WorkloadResult,
    f3: WorkloadResult,
    baseline_tps: float,
) -> None:
    actual_good = (
        f3.failures == 0 and f3.p99_ms <= P99_SLO_MS and f3.throughput_tps >= baseline_tps * 0.95
    )
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.early_stop_decisions
            (early_stop_id,campaign_id,candidate_key,fidelity,protected_seconds,
             observed_throughput_tps,baseline_throughput_tps,stopped,reason,
             retrospective_false_stop) VALUES (%s,%s,%s,2,5,%s,%s,%s,%s,%s)""",
            (
                uuid.uuid4(),
                campaign_id,
                _candidate_key(candidate.configuration),
                f2.throughput_tps,
                baseline_tps,
                decision.stopped,
                decision.reason,
                decision.stopped and actual_good,
            ),
        )
        conn.commit()


def run_fidelity_pilot(
    settings: Settings,
    candidates: int = 4,
    f2_seconds: int = 5,
    f3_seconds: int = 30,
    f4_replicates: int = 2,
    seed: int = 20260715,
) -> FidelityPilotResult:
    if candidates < 4 or f2_seconds < 3 or f3_seconds < 10 or f4_replicates < 2:
        raise ValueError("fidelity pilot budget is below its evidence floor")
    baseline_tps = _baseline_tps(settings)
    campaign_id = uuid.uuid4()
    started_campaign = time.monotonic()
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.campaigns
            (campaign_id,name,mode,status,objective_definition,constraint_definition)
            VALUES (%s,%s,'MULTI_FIDELITY','RUNNING',%s,%s)""",
            (
                campaign_id,
                f"fidelity-pilot-{seed}",
                Jsonb({"maximize": "throughput_tps"}),
                Jsonb({"p99_ms_max": P99_SLO_MS, "failures_max": 0}),
            ),
        )
        conn.commit()

    pairs: list[FidelityPair] = []
    promotion_ids: list[uuid.UUID] = []
    for index, candidate in enumerate(sobol_candidates(candidates, seed)):
        f0_started = time.monotonic()
        metadata = discover_knobs(settings, set(candidate.configuration))
        validate_candidate(candidate.configuration, metadata)
        _persist_fidelity(
            settings,
            campaign_id,
            candidate,
            0,
            0,
            time.monotonic() - f0_started,
            "static-validation",
        )
        application = apply_configuration(settings, candidate.configuration)
        try:
            f1_started = time.monotonic()
            planner_cost = _planner_cost(settings)
            _persist_fidelity(
                settings,
                campaign_id,
                candidate,
                1,
                0,
                time.monotonic() - f1_started,
                "planner-cost-proxy-not-runtime",
                planner_cost=planner_cost,
            )
            f2_settings = settings.model_copy(
                update={
                    "benchmark_warmup_seconds": 2,
                    "benchmark_duration_seconds": f2_seconds,
                    "benchmark_seed": seed + index,
                }
            )
            f2_trial, _path, f2 = benchmark_default(
                f2_settings, candidate.configuration, "f2-reduced", 2, "f2-reduced"
            )
            _persist_fidelity(
                settings,
                campaign_id,
                candidate,
                2,
                0,
                f2.duration_seconds,
                "reduced-runtime",
                f2,
                f2_trial,
                feasible=f2.failures == 0 and f2.p99_ms <= P99_SLO_MS,
            )
            promotion = decide_promotion(f2, baseline_tps)
            promotion_ids.append(
                _persist_promotion(settings, campaign_id, candidate, promotion, f2, baseline_tps)
            )
            f3_settings = settings.model_copy(
                update={
                    "benchmark_warmup_seconds": 5,
                    "benchmark_duration_seconds": f3_seconds,
                    "benchmark_seed": seed + index,
                }
            )
            f3_trial, _path, f3 = benchmark_default(
                f3_settings, candidate.configuration, "f3-full", 3, "f3-full"
            )
            _persist_fidelity(
                settings,
                campaign_id,
                candidate,
                3,
                0,
                f3.duration_seconds,
                "full-runtime-paired-pilot",
                f3,
                f3_trial,
                feasible=f3.failures == 0 and f3.p99_ms <= P99_SLO_MS,
            )
            stop = decide_early_stop(f2.throughput_tps, f2.p99_ms, baseline_tps, f2_seconds)
            _persist_early_stop(settings, campaign_id, candidate, stop, f2, f3, baseline_tps)
            pairs.append(FidelityPair(candidate, f2, f3, promotion.promoted))
        finally:
            rollback_configuration(
                settings, application.application_id, f"restore after fidelity pair {index}"
            )

    f2_values = [pair.f2.throughput_tps for pair in pairs]
    f3_values = [pair.f3.throughput_tps for pair in pairs]
    spearman = float(spearmanr(f2_values, f3_values).statistic)
    kendall = float(kendalltau(f2_values, f3_values).statistic)
    mean_bias = statistics.mean(f2 - f3 for f2, f3 in zip(f2_values, f3_values, strict=True))
    confusion = {"TP": 0, "FP": 0, "TN": 0, "FN": 0}
    for pair, promotion_id in zip(pairs, promotion_ids, strict=True):
        actual_good = (
            pair.f3.failures == 0
            and pair.f3.p99_ms <= P99_SLO_MS
            and pair.f3.throughput_tps >= baseline_tps * 0.95
        )
        classification = (
            "TP"
            if pair.promoted and actual_good
            else "FP"
            if pair.promoted
            else "FN"
            if actual_good
            else "TN"
        )
        confusion[classification] += 1
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE charm_control.promotion_decisions
                SET retrospective_actual_good=%s,classification=%s WHERE promotion_id=%s""",
                (actual_good, classification, promotion_id),
            )
            conn.commit()

    champion = max(pairs, key=lambda pair: pair.f3.throughput_tps).candidate
    f4_values: list[float] = []
    application = apply_configuration(settings, champion.configuration)
    try:
        for replicate in range(f4_replicates):
            f4_settings = settings.model_copy(
                update={
                    "benchmark_warmup_seconds": 5,
                    "benchmark_duration_seconds": f3_seconds,
                    "benchmark_seed": seed + 100 + replicate,
                }
            )
            trial_id, _path, result = benchmark_default(
                f4_settings, champion.configuration, "f4-validation", 4, "f4-validation"
            )
            _persist_fidelity(
                settings,
                campaign_id,
                champion,
                4,
                replicate,
                result.duration_seconds,
                "repeated-full-validation",
                result,
                trial_id,
                feasible=result.failures == 0 and result.p99_ms <= P99_SLO_MS,
            )
            f4_values.append(result.throughput_tps)
    finally:
        rollback_configuration(settings, application.application_id, "restore after F4 validation")

    early_enabled = early_stopping_gate(len(pairs), spearman, confusion["FN"])
    elapsed = time.monotonic() - started_campaign
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO charm_control.fidelity_reports
            (report_id,campaign_id,paired_candidates,spearman_correlation,kendall_correlation,
             mean_f2_f3_bias_tps,promotion_true_positive,promotion_false_positive,
             promotion_true_negative,promotion_false_negative,early_stopping_enabled,
             full_evaluations_avoided,measured_wall_clock_seconds,details)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s)""",
            (
                uuid.uuid4(),
                campaign_id,
                len(pairs),
                spearman,
                kendall,
                mean_bias,
                confusion["TP"],
                confusion["FP"],
                confusion["TN"],
                confusion["FN"],
                early_enabled,
                elapsed,
                Jsonb(
                    {
                        "baseline_tps": baseline_tps,
                        "f2_seconds": f2_seconds,
                        "f3_seconds": f3_seconds,
                        "f4_replicates": f4_replicates,
                        "note": "all F3 runs forced for paired pilot; no evaluations avoided",
                    }
                ),
            ),
        )
        cur.execute(
            """UPDATE charm_control.campaigns SET status='COMPLETED',
            updated_at=clock_timestamp() WHERE campaign_id=%s""",
            (campaign_id,),
        )
        conn.commit()
    return FidelityPilotResult(
        campaign_id,
        len(pairs),
        spearman,
        kendall,
        mean_bias,
        confusion,
        early_enabled,
        champion.configuration,
        tuple(f4_values),
        elapsed,
    )


def result_json(result: FidelityPilotResult) -> str:
    return json.dumps(
        {
            "campaign_id": str(result.campaign_id),
            "paired_candidates": result.paired_candidates,
            "spearman": result.spearman,
            "kendall": result.kendall,
            "mean_f2_f3_bias_tps": result.mean_bias_tps,
            "promotion_confusion": result.promotion_confusion,
            "early_stopping_enabled": result.early_stopping_enabled,
            "champion_configuration": result.champion_configuration,
            "f4_throughputs": result.f4_throughputs,
            "wall_clock_seconds": result.wall_clock_seconds,
        },
        indent=2,
    )
