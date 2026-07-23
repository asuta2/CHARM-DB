from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.costing import COST_VARIANTS
from charmdb.db import connect

COORDINATION_STRATEGIES = (
    "knob_only",
    "index_only",
    "knobs_then_indexes",
    "indexes_then_knobs",
    "alternating",
    "coordinated",
)


@dataclass(frozen=True)
class KnobOption:
    configuration: dict[str, str]
    predicted_score: float


@dataclass(frozen=True)
class IndexOption:
    candidate_ids: tuple[uuid.UUID, ...]
    predicted_score: float


@dataclass(frozen=True)
class JointAction:
    knob_configuration: dict[str, str]
    index_candidate_ids: tuple[uuid.UUID, ...]
    knob_score: float
    index_score: float
    interaction_score: float

    @property
    def selected_score(self) -> float:
        return self.knob_score + self.index_score + self.interaction_score


def _knob_key(configuration: dict[str, str]) -> str:
    return json.dumps(configuration, sort_keys=True)


def _ranked_knobs(options: list[KnobOption]) -> list[KnobOption]:
    return sorted(options, key=lambda item: item.predicted_score, reverse=True)


def _ranked_indexes(options: list[IndexOption]) -> list[IndexOption]:
    return sorted(options, key=lambda item: item.predicted_score, reverse=True)


def select_action_schedule(
    strategy: str,
    knobs: list[KnobOption],
    indexes: list[IndexOption],
    budget: int,
    interaction_scores: dict[tuple[str, tuple[uuid.UUID, ...]], float] | None = None,
) -> list[JointAction]:
    if strategy not in COORDINATION_STRATEGIES:
        raise ValueError(f"unsupported coordination strategy {strategy}")
    if budget < 1:
        raise ValueError("coordination budget must be positive")
    if not knobs or not indexes:
        raise ValueError("comparison requires both bounded knob and index option sets")
    ranked_knobs = _ranked_knobs(knobs)
    ranked_indexes = _ranked_indexes(indexes)
    interactions = interaction_scores or {}

    def knob_action(position: int) -> JointAction:
        knob = ranked_knobs[position % len(ranked_knobs)]
        return JointAction(knob.configuration, (), knob.predicted_score, 0.0, 0.0)

    def index_action(position: int) -> JointAction:
        index = ranked_indexes[position % len(ranked_indexes)]
        return JointAction({}, index.candidate_ids, 0.0, index.predicted_score, 0.0)

    if strategy == "knob_only":
        return [knob_action(position) for position in range(budget)]
    if strategy == "index_only":
        return [index_action(position) for position in range(budget)]
    if strategy in {"knobs_then_indexes", "indexes_then_knobs"}:
        first_count = (budget + 1) // 2
        knob_actions = [knob_action(position) for position in range(first_count)]
        index_actions = [index_action(position) for position in range(budget - first_count)]
        return (
            knob_actions + index_actions
            if strategy == "knobs_then_indexes"
            else index_actions + knob_actions
        )
    if strategy == "alternating":
        return [
            knob_action(position // 2) if position % 2 == 0 else index_action(position // 2)
            for position in range(budget)
        ]

    joint = [
        JointAction(
            knob.configuration,
            index.candidate_ids,
            knob.predicted_score,
            index.predicted_score,
            interactions.get((_knob_key(knob.configuration), index.candidate_ids), 0.0),
        )
        for knob in ranked_knobs
        for index in ranked_indexes
    ]
    ordered = sorted(joint, key=lambda item: item.selected_score, reverse=True)
    if len(ordered) < budget:
        raise ValueError("coordinated option space is smaller than the equal budget")
    return ordered[:budget]


def persist_action_schedule(
    settings: Settings,
    campaign_id: uuid.UUID,
    strategy: str,
    knobs: list[KnobOption],
    indexes: list[IndexOption],
    budget: int,
    training_observation_ids: list[uuid.UUID],
    interaction_scores: dict[tuple[str, tuple[uuid.UUID, ...]], float] | None = None,
    experiment_arm_id: uuid.UUID | None = None,
    cost_variant: str = "no_cost",
    predicted_operational_costs: list[dict[str, object]] | None = None,
) -> list[uuid.UUID]:
    if cost_variant not in COST_VARIANTS:
        raise ValueError(f"unsupported cost variant {cost_variant}")
    actions = select_action_schedule(
        strategy, knobs, indexes, budget, interaction_scores=interaction_scores
    )
    costs = predicted_operational_costs or [{} for _ in actions]
    if len(costs) != len(actions):
        raise ValueError("predicted operational costs must match the action budget")
    decision_ids: list[uuid.UUID] = []
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        for position, (action, predicted_cost) in enumerate(zip(actions, costs, strict=True)):
            decision_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"charmdb:{campaign_id}:coordination:{strategy}:{position}",
            )
            component_scores = {"knob": action.knob_score, "index": action.index_score}
            cur.execute(
                """
                INSERT INTO charm_control.coordination_decisions
                (coordination_id,campaign_id,strategy,budget_position,knob_configuration,
                 index_candidate_ids,component_scores,interaction_score,selected_score,
                 training_observation_ids,experiment_arm_id,cost_variant,
                 predicted_operational_cost)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (campaign_id,strategy,budget_position) DO NOTHING
                """,
                (
                    decision_id,
                    campaign_id,
                    strategy,
                    position,
                    Jsonb(action.knob_configuration),
                    list(action.index_candidate_ids),
                    Jsonb(component_scores),
                    action.interaction_score,
                    action.selected_score,
                    Jsonb([str(item) for item in training_observation_ids]),
                    experiment_arm_id,
                    cost_variant,
                    Jsonb(predicted_cost),
                ),
            )
            cur.execute(
                """
                SELECT coordination_id,knob_configuration,index_candidate_ids,component_scores,
                       interaction_score,selected_score,training_observation_ids,experiment_arm_id,
                       cost_variant,predicted_operational_cost
                FROM charm_control.coordination_decisions
                WHERE campaign_id=%s AND strategy=%s AND budget_position=%s
                """,
                (campaign_id, strategy, position),
            )
            stored = cur.fetchone()
            if stored is None:
                raise RuntimeError(
                    f"coordination decision was not persisted at position {position}"
                )
            expected = {
                "knob_configuration": action.knob_configuration,
                "index_candidate_ids": list(action.index_candidate_ids),
                "component_scores": component_scores,
                "interaction_score": action.interaction_score,
                "selected_score": action.selected_score,
                "training_observation_ids": [str(item) for item in training_observation_ids],
                "experiment_arm_id": experiment_arm_id,
                "cost_variant": cost_variant,
                "predicted_operational_cost": predicted_cost,
            }
            actual = {key: stored[key] for key in expected}
            if actual != expected:
                raise RuntimeError(
                    f"persisted coordination decision differs at position {position}: "
                    f"expected={expected}, actual={actual}"
                )
            decision_ids.append(stored["coordination_id"])
        conn.commit()
    return decision_ids


def coordination_history(settings: Settings, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT coordination_id,campaign_id,strategy,budget_position,knob_configuration,
                   index_candidate_ids,component_scores,interaction_score,selected_score,
                   training_observation_ids,experiment_arm_id,cost_variant,
                   predicted_operational_cost,created_at
            FROM charm_control.coordination_decisions
            WHERE campaign_id=%s ORDER BY strategy,budget_position
            """,
            (campaign_id,),
        )
        return [dict(row) for row in cur.fetchall()]
