from __future__ import annotations

import uuid
from dataclasses import dataclass

from charmdb.config import Settings
from charmdb.db import connect
from charmdb.manifest_preflight import validate_dataset_restore


@dataclass(frozen=True)
class ArmDatasetRestore:
    arm_id: uuid.UUID
    preflight_id: uuid.UUID
    validation_id: uuid.UUID
    manifest_sha256: str
    restored: bool


def ensure_arm_dataset_restored(
    settings: Settings,
    arm_id: uuid.UUID,
    preflight_id: uuid.UUID,
    manifest_sha256: str,
) -> ArmDatasetRestore:
    with connect(settings.control_dsn) as lock_conn, lock_conn.cursor() as lock_cur:
        lock_cur.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (str(arm_id),))
        lock_cur.execute(
            """SELECT arm_id,preflight_id,validation_id,manifest_sha256
               FROM charm_control.experiment_arm_dataset_restores WHERE arm_id=%s""",
            (arm_id,),
        )
        existing = lock_cur.fetchone()
        if existing is not None:
            if (
                existing["preflight_id"] != preflight_id
                or str(existing["manifest_sha256"]) != manifest_sha256
            ):
                raise ValueError("arm dataset restore lineage conflicts with the frozen manifest")
            return ArmDatasetRestore(
                arm_id,
                preflight_id,
                uuid.UUID(str(existing["validation_id"])),
                manifest_sha256,
                False,
            )
        lock_cur.execute(
            """SELECT status,campaign_id,manifest_sha256,execution_manifest
               FROM charm_control.experiment_arms WHERE arm_id=%s FOR UPDATE""",
            (arm_id,),
        )
        arm = lock_cur.fetchone()
        if arm is None:
            raise ValueError(f"unknown experiment arm {arm_id}")
        if str(arm["status"]) != "PREPARED":
            raise ValueError("a first arm dataset restore requires a PREPARED experiment arm")
        if arm["campaign_id"] is None:
            raise ValueError("prepared experiment arm has no linked campaign")
        if str(arm["manifest_sha256"] or "") != manifest_sha256:
            raise ValueError("arm dataset restore manifest does not match the frozen arm")
        manifest = dict(arm["execution_manifest"] or {})
        execution = dict(manifest.get("execution") or {})
        lineage = dict(execution.get("evidence_lineage") or {})
        if str(lineage.get("preflight_id", "")) != str(preflight_id):
            raise ValueError("arm dataset restore preflight does not match execution lineage")
        registry = dict(manifest.get("registry") or {})
        if str(registry.get("arm_id", "")) != str(arm_id):
            raise ValueError("frozen manifest registry does not match the experiment arm")
        campaign_id = uuid.UUID(str(arm["campaign_id"]))
        lock_cur.execute(
            """SELECT status,mode,settings FROM charm_control.campaigns
               WHERE campaign_id=%s FOR UPDATE""",
            (campaign_id,),
        )
        campaign = lock_cur.fetchone()
        if campaign is None:
            raise ValueError("prepared experiment arm campaign is missing")
        campaign_settings = dict(campaign["settings"] or {})
        if (
            str(campaign["status"]) != "CREATED"
            or str(campaign["mode"]) != "EXPERIMENT"
            or str(campaign_settings.get("experiment_arm_id", "")) != str(arm_id)
            or str(campaign_settings.get("manifest_sha256", "")) != manifest_sha256
        ):
            raise ValueError("prepared campaign does not match the frozen experiment arm")
        lock_cur.execute(
            """SELECT
                 (SELECT count(*) FROM charm_control.experiment_search_recommendations
                  WHERE arm_id=%s) AS recommendations,
                 (SELECT count(*) FROM charm_control.trials
                  WHERE campaign_id=%s) AS trials,
                 (SELECT count(*) FROM charm_control.experiment_budget_entries
                  WHERE arm_id=%s) AS budget_entries""",
            (arm_id, campaign_id, arm_id),
        )
        work = lock_cur.fetchone()
        if work is None or any(
            int(work[name]) != 0 for name in ("recommendations", "trials", "budget_entries")
        ):
            raise ValueError("experiment work exists without a durable pre-arm dataset restore")
        validation = validate_dataset_restore(
            settings,
            preflight_id,
            permitted_campaign_id=campaign_id,
        )
        if not validation.passed:
            raise RuntimeError("pre-arm dataset restore validation failed")
        lock_cur.execute(
            """INSERT INTO charm_control.experiment_arm_dataset_restores
               (arm_id,preflight_id,validation_id,manifest_sha256)
               VALUES (%s,%s,%s,%s)""",
            (arm_id, preflight_id, validation.validation_id, manifest_sha256),
        )
        lock_conn.commit()
    return ArmDatasetRestore(arm_id, preflight_id, validation.validation_id, manifest_sha256, True)
