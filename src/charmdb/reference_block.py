from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import Any

from psycopg.types.json import Jsonb

from charmdb.config import Settings
from charmdb.db import connect
from charmdb.manifest_preflight import validate_dataset_restore
from charmdb.worker import (
    TERMINAL_STATES,
    control_campaign,
    create_baseline_benchmark_trial,
    run_once,
)


@dataclass(frozen=True)
class F3ReferenceBlockResult:
    block_id: uuid.UUID
    preflight_id: uuid.UUID
    campaign_id: uuid.UUID
    completed_trials: int
    reference_wall_clock_seconds: float
    passed: bool
    post_block_restore_validation_id: uuid.UUID


def _load_preflight(settings: Settings, preflight_id: uuid.UUID) -> dict[str, Any]:
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT preflight_id,dataset_content_sha256,unresolved,evidence
               FROM charm_control.experiment_manifest_preflights WHERE preflight_id=%s""",
            (preflight_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"unknown manifest preflight {preflight_id}")
        cur.execute(
            """SELECT count(*) AS count
               FROM charm_control.experiment_dataset_restore_validations
               WHERE preflight_id=%s AND passed""",
            (preflight_id,),
        )
        restore_count = int(cur.fetchone()["count"])  # type: ignore[index]
    if restore_count < 1:
        raise ValueError("F3 reference block requires a passed dataset restore validation")
    if row["dataset_content_sha256"] is None:
        raise ValueError("F3 reference block requires a content-hashed dataset snapshot")
    return dict(row)


def _prepare_reference_campaign(
    settings: Settings,
    preflight_id: uuid.UUID,
    profile: dict[str, int],
    repetitions: int,
    seed: int,
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    block_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:f3-reference:{preflight_id}")
    campaign_id = uuid.uuid5(uuid.NAMESPACE_URL, f"charmdb:f3-reference-campaign:{preflight_id}")
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO charm_control.campaigns
            (campaign_id,name,mode,status,objective_definition,constraint_definition,
             settings,failure_limit)
            VALUES (%s,%s,'F3_REFERENCE','CREATED',%s,%s,%s,%s)
            ON CONFLICT (campaign_id) DO NOTHING
            """,
            (
                campaign_id,
                f"f3-reference-{preflight_id}",
                Jsonb({"measure": "default_F3_wall_clock"}),
                Jsonb({"measurement_complete": True}),
                Jsonb({"preflight_id": str(preflight_id), "profile": profile}),
                repetitions,
            ),
        )
        if cur.rowcount:
            cur.execute(
                """INSERT INTO charm_control.campaign_events
                   (campaign_id,event_type,previous_status,new_status,actor,reason,details)
                   VALUES (%s,'CREATE',NULL,'CREATED','reference-orchestrator',
                           'F3 reference campaign created',%s)""",
                (campaign_id, Jsonb({"preflight_id": str(preflight_id)})),
            )
        cur.execute(
            """
            INSERT INTO charm_control.experiment_f3_reference_blocks
            (block_id,preflight_id,campaign_id,warmup_seconds,measurement_seconds,
             concurrency,required_trials)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (preflight_id) DO NOTHING
            """,
            (
                block_id,
                preflight_id,
                campaign_id,
                profile["warmup_seconds"],
                profile["measurement_seconds"],
                profile["concurrency"],
                repetitions,
            ),
        )
        cur.execute(
            """SELECT warmup_seconds,measurement_seconds,concurrency,required_trials,campaign_id
               FROM charm_control.experiment_f3_reference_blocks WHERE preflight_id=%s""",
            (preflight_id,),
        )
        existing = cur.fetchone()
        if existing is None or any(
            int(existing[name]) != value
            for name, value in (
                ("warmup_seconds", profile["warmup_seconds"]),
                ("measurement_seconds", profile["measurement_seconds"]),
                ("concurrency", profile["concurrency"]),
                ("required_trials", repetitions),
            )
        ):
            raise ValueError("existing F3 reference block has a different frozen profile")
        if existing["campaign_id"] != campaign_id:
            raise ValueError("existing F3 reference block has a different campaign")
        conn.commit()
    trial_ids = [
        create_baseline_benchmark_trial(
            settings,
            campaign_id,
            seed + position,
            f"f3-reference:{preflight_id}:{seed}:{position}",
            profile["warmup_seconds"],
            profile["measurement_seconds"],
            profile["concurrency"],
            3,
            20.0,
            3,
        )
        for position in range(repetitions)
    ]
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_f3_reference_blocks SET trial_ids=%s
               WHERE block_id=%s""",
            (Jsonb([str(item) for item in trial_ids]), block_id),
        )
        cur.execute(
            "SELECT status FROM charm_control.campaigns WHERE campaign_id=%s", (campaign_id,)
        )
        status = str(cur.fetchone()["status"])  # type: ignore[index]
        conn.commit()
    if status in {"CREATED", "PAUSED"}:
        control_campaign(
            settings,
            campaign_id,
            "resume",
            "execute exact-profile F3 reference block",
            "reference-orchestrator",
        )
    elif status != "RUNNING":
        raise ValueError(f"F3 reference campaign cannot resume from {status}")
    return block_id, campaign_id, trial_ids


def _run_reference_trials(settings: Settings, campaign_id: uuid.UUID) -> None:
    while True:
        with connect(settings.control_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) AS incomplete,min(next_attempt_at) AS next_attempt
                   FROM charm_control.trials WHERE campaign_id=%s AND completed_at IS NULL""",
                (campaign_id,),
            )
            pending = cur.fetchone()
        if pending is None or int(pending["incomplete"]) == 0:
            return
        try:
            result = run_once(
                settings,
                owner="f3-reference-worker",
                lease_seconds=120,
                campaign_id=campaign_id,
            )
        except Exception:
            result = None
        if result is None or not result.claimed:
            delay = max(0.25, (pending["next_attempt"] - datetime.now(UTC)).total_seconds())
            time.sleep(min(delay, 5.0))


def run_f3_reference_block(
    settings: Settings,
    preflight_id: uuid.UUID,
    repetitions: int = 5,
    seed: int = 20260730,
) -> F3ReferenceBlockResult:
    if repetitions < 5:
        raise ValueError("F3 reference block requires at least five repetitions")
    preflight = _load_preflight(settings, preflight_id)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT block_id,campaign_id,completed_trials,reference_wall_clock_seconds,passed,
                      details FROM charm_control.experiment_f3_reference_blocks
               WHERE preflight_id=%s""",
            (preflight_id,),
        )
        completed = cur.fetchone()
    if completed is not None and bool(completed["passed"]):
        restore = validate_dataset_restore(settings, preflight_id)
        return F3ReferenceBlockResult(
            completed["block_id"],
            preflight_id,
            completed["campaign_id"],
            int(completed["completed_trials"]),
            float(completed["reference_wall_clock_seconds"]),
            True,
            restore.validation_id,
        )
    validate_dataset_restore(settings, preflight_id)
    evidence = dict(preflight["evidence"])
    profile = {
        name: int(evidence["proposed_execution_profile"][name])
        for name in ("warmup_seconds", "measurement_seconds", "concurrency")
    }
    block_id, campaign_id, trial_ids = _prepare_reference_campaign(
        settings, preflight_id, profile, repetitions, seed
    )
    _run_reference_trials(settings, campaign_id)
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT trial_id,state,created_at,started_at,completed_at,objective_values,
                      constraint_values,failure_type
               FROM charm_control.trials WHERE trial_id=ANY(%s) ORDER BY created_at""",
            (trial_ids,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    valid = [
        row
        for row in rows
        if row["completed_at"] is not None
        and str(row["state"]) in TERMINAL_STATES
        and row["objective_values"] is not None
        and row["constraint_values"] is not None
    ]
    if len(valid) != repetitions:
        raise RuntimeError(f"F3 reference block has {len(valid)}/{repetitions} valid trials")
    elapsed = [
        max(0.0, (row["completed_at"] - (row["started_at"] or row["created_at"])).total_seconds())
        for row in valid
    ]
    reference = float(median(elapsed))
    details = {
        "trial_states": {str(row["trial_id"]): row["state"] for row in rows},
        "trial_wall_clock_seconds": {
            str(row["trial_id"]): value for row, value in zip(valid, elapsed, strict=True)
        },
        "seed": seed,
    }
    with connect(settings.control_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE charm_control.experiment_f3_reference_blocks
               SET completed_trials=%s,reference_wall_clock_seconds=%s,passed=true,details=%s,
                   completed_at=clock_timestamp() WHERE block_id=%s""",
            (len(valid), reference, Jsonb(details), block_id),
        )
        cur.execute(
            """SELECT status FROM charm_control.campaigns WHERE campaign_id=%s FOR UPDATE""",
            (campaign_id,),
        )
        campaign_status = str(cur.fetchone()["status"])  # type: ignore[index]
        if campaign_status != "COMPLETED":
            cur.execute(
                """UPDATE charm_control.campaigns SET status='COMPLETED',
                          updated_at=clock_timestamp() WHERE campaign_id=%s""",
                (campaign_id,),
            )
            cur.execute(
                """INSERT INTO charm_control.campaign_events
                   (campaign_id,event_type,previous_status,new_status,actor,reason,details)
                   VALUES (%s,'COMPLETE',%s,'COMPLETED','reference-orchestrator',
                           'five exact-profile F3 references completed',%s)""",
                (campaign_id, campaign_status, Jsonb({"reference_wall_clock_seconds": reference})),
            )
        unresolved = tuple(
            str(item)
            for item in preflight["unresolved"]
            if not str(item).startswith("fewer than five default F3 references")
        )
        cur.execute(
            """UPDATE charm_control.experiment_manifest_preflights
               SET eligible=%s,unresolved=%s WHERE preflight_id=%s""",
            (not unresolved, Jsonb(list(unresolved)), preflight_id),
        )
        conn.commit()
    restore = validate_dataset_restore(settings, preflight_id)
    return F3ReferenceBlockResult(
        block_id, preflight_id, campaign_id, len(valid), reference, True, restore.validation_id
    )
