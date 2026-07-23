from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections import defaultdict
from dataclasses import dataclass

from charmdb.fingerprinting import TRANSFORM_VERSION, weighted_distance
from charmdb.optimizer import Candidate, sobol_candidates

KNOB_SPACE_VERSION = "reload-knobs-v1"
TRANSFER_METHODS = (
    "no_transfer",
    "historical_champion",
    "similarity_weighted",
    "rank_weighted_ensemble",
)


@dataclass(frozen=True)
class CompatibilityKey:
    postgres_major: int
    schema_signature: str
    resource_signature: str
    knob_space_version: str
    objective_definition: str
    constraint_definition: str
    fingerprint_transform_version: str
    fidelity: int = 3


@dataclass(frozen=True)
class HistoryRecord:
    history_id: uuid.UUID
    source_campaign_id: uuid.UUID
    compatibility: CompatibilityKey
    configuration: dict[str, str]
    throughput_tps: float
    p99_ms: float
    feasible: bool
    fingerprint_vector: tuple[float, ...]


@dataclass(frozen=True)
class CompatibleHistory:
    record: HistoryRecord
    fingerprint_distance: float
    similarity: float


@dataclass(frozen=True)
class TransferOutcome:
    method: str
    budget: int
    best_feasible_tps: float | None
    trials_to_recovery: int | None
    unsafe_trials: int


@dataclass(frozen=True)
class NegativeTransferResult:
    negative: bool
    reasons: tuple[str, ...]


def canonical_definition(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def signature(value: object) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def compatible_history(
    records: list[HistoryRecord],
    target: CompatibilityKey,
    current_fingerprint: tuple[float, ...],
    similarity_threshold: float,
) -> tuple[list[CompatibleHistory], dict[str, int]]:
    if similarity_threshold < 0:
        raise ValueError("similarity threshold cannot be negative")
    accepted: list[CompatibleHistory] = []
    rejected: dict[str, int] = defaultdict(int)
    exact_fields = (
        "postgres_major",
        "schema_signature",
        "resource_signature",
        "knob_space_version",
        "objective_definition",
        "constraint_definition",
        "fingerprint_transform_version",
        "fidelity",
    )
    for record in records:
        mismatch = next(
            (
                field
                for field in exact_fields
                if getattr(record.compatibility, field) != getattr(target, field)
            ),
            None,
        )
        if mismatch is not None:
            rejected[f"incompatible_{mismatch}"] += 1
            continue
        distance = weighted_distance(record.fingerprint_vector, current_fingerprint)
        if distance > similarity_threshold:
            rejected["fingerprint_distance"] += 1
            continue
        accepted.append(
            CompatibleHistory(
                record, distance, math.exp(-distance / max(similarity_threshold, 1e-9))
            )
        )
    return accepted, dict(rejected)


def _configuration_key(configuration: dict[str, str]) -> str:
    return json.dumps(configuration, sort_keys=True)


def _rank_scores(history: list[CompatibleHistory]) -> dict[uuid.UUID, float]:
    campaigns: dict[uuid.UUID, list[CompatibleHistory]] = defaultdict(list)
    for item in history:
        campaigns[item.record.source_campaign_id].append(item)
    scores: dict[uuid.UUID, float] = {}
    for values in campaigns.values():
        ordered = sorted(values, key=lambda item: item.record.throughput_tps)
        denominator = max(1, len(ordered) - 1)
        for rank, item in enumerate(ordered):
            scores[item.record.history_id] = rank / denominator
    return scores


def _history_order(method: str, history: list[CompatibleHistory]) -> list[CompatibleHistory]:
    feasible = [item for item in history if item.record.feasible]
    if method == "historical_champion":
        return sorted(feasible, key=lambda item: item.record.throughput_tps, reverse=True)
    ranks = _rank_scores(feasible)
    if method == "similarity_weighted":
        return sorted(
            feasible,
            key=lambda item: item.similarity * ranks[item.record.history_id],
            reverse=True,
        )
    if method == "rank_weighted_ensemble":
        by_configuration: dict[str, list[CompatibleHistory]] = defaultdict(list)
        for item in feasible:
            by_configuration[_configuration_key(item.record.configuration)].append(item)

        def ensemble_score(item: CompatibleHistory) -> float:
            values = by_configuration[_configuration_key(item.record.configuration)]
            weighted = sum(value.similarity * ranks[value.record.history_id] for value in values)
            total_weight = sum(value.similarity for value in values)
            # Shrink sparse history toward a neutral rank rather than overtrusting one campaign.
            return (weighted + 0.5) / (total_weight + 1.0)

        return sorted(feasible, key=ensemble_score, reverse=True)
    raise ValueError(f"unsupported transfer method {method}")


def warm_start_candidates(
    method: str,
    history: list[CompatibleHistory],
    budget: int,
    seed: int,
) -> list[Candidate]:
    if method not in TRANSFER_METHODS:
        raise ValueError(f"unsupported transfer method {method}")
    if budget < 1:
        raise ValueError("transfer comparison budget must be positive")
    result: list[Candidate] = []
    seen: set[str] = set()
    if method != "no_transfer":
        for item in _history_order(method, history):
            key = _configuration_key(item.record.configuration)
            if key in seen:
                continue
            seen.add(key)
            result.append(
                Candidate(
                    (
                        (float(item.record.configuration["random_page_cost"]) - 1) / 3,
                        (math.log2(int(item.record.configuration["work_mem"])) - 10) / 5,
                        int(item.record.configuration["effective_io_concurrency"]) / 200,
                    ),
                    item.record.configuration,
                )
            )
            if len(result) == budget:
                return result
    for candidate in sobol_candidates(budget * 2, seed):
        key = _configuration_key(candidate.configuration)
        if key not in seen:
            seen.add(key)
            result.append(candidate)
        if len(result) == budget:
            break
    if len(result) != budget:
        raise RuntimeError("could not construct a unique equal-budget warm start")
    return result


def classify_negative_transfer(
    outcome: TransferOutcome,
    no_transfer: TransferOutcome,
    practical_tps_margin: float = 0.02,
) -> NegativeTransferResult:
    if outcome.budget != no_transfer.budget:
        raise ValueError("negative transfer requires equal realized budgets")
    if not 0 <= practical_tps_margin < 1:
        raise ValueError("practical throughput margin must be in [0, 1)")
    reasons: list[str] = []
    if no_transfer.best_feasible_tps is not None:
        if outcome.best_feasible_tps is None:
            reasons.append("no feasible transferred candidate")
        elif outcome.best_feasible_tps < no_transfer.best_feasible_tps * (1 - practical_tps_margin):
            reasons.append("best feasible throughput below practical margin")
    if (
        no_transfer.trials_to_recovery is not None
        and outcome.trials_to_recovery is not None
        and outcome.trials_to_recovery > no_transfer.trials_to_recovery
    ):
        reasons.append("slower recovery than no transfer")
    if outcome.unsafe_trials > no_transfer.unsafe_trials:
        reasons.append("more unsafe trials than no transfer")
    return NegativeTransferResult(bool(reasons), tuple(reasons))


def default_compatibility(
    postgres_major: int,
    schema_signature: str,
    resource_signature: str,
) -> CompatibilityKey:
    return CompatibilityKey(
        postgres_major=postgres_major,
        schema_signature=schema_signature,
        resource_signature=resource_signature,
        knob_space_version=KNOB_SPACE_VERSION,
        objective_definition=canonical_definition({"maximize": "throughput_tps"}),
        constraint_definition=canonical_definition({"p99_ms_max": 20.0, "failures_max": 0}),
        fingerprint_transform_version=TRANSFORM_VERSION,
        fidelity=3,
    )
