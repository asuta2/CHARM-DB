import uuid
from pathlib import Path

import pytest

from charmdb.config import get_settings
from charmdb.coordination import (
    IndexOption,
    KnobOption,
    coordination_history,
    persist_action_schedule,
)
from charmdb.worker import control_campaign, create_campaign

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not Path(".env").exists(), reason="integration environment not configured")
def test_coordination_schedule_is_persisted_idempotently() -> None:
    settings = get_settings()
    campaign_id = create_campaign(
        settings,
        f"coordination-persistence-{uuid.uuid4().hex[:8]}",
        "DESIGN",
        {},
        {},
        actor="pytest",
    )
    knobs = [
        KnobOption({"random_page_cost": "2"}, 3.0),
        KnobOption({"random_page_cost": "3"}, 2.0),
    ]
    indexes = [
        IndexOption((uuid.uuid5(uuid.NAMESPACE_URL, "coordination-index-1"),), 2.5),
        IndexOption((uuid.uuid5(uuid.NAMESPACE_URL, "coordination-index-2"),), 1.5),
    ]
    training_ids = [uuid.uuid5(uuid.NAMESPACE_URL, "coordination-training-1")]
    interaction = {
        ('{"random_page_cost": "3"}', indexes[1].candidate_ids): 10.0,
    }

    first = persist_action_schedule(
        settings,
        campaign_id,
        "coordinated",
        knobs,
        indexes,
        budget=3,
        training_observation_ids=training_ids,
        interaction_scores=interaction,
        cost_variant="complete",
        predicted_operational_costs=[{"total": 1.0}, {"total": 2.0}, {"total": 3.0}],
    )
    repeated = persist_action_schedule(
        settings,
        campaign_id,
        "coordinated",
        knobs,
        indexes,
        budget=3,
        training_observation_ids=training_ids,
        interaction_scores=interaction,
        cost_variant="complete",
        predicted_operational_costs=[{"total": 1.0}, {"total": 2.0}, {"total": 3.0}],
    )
    history = coordination_history(settings, campaign_id)

    assert repeated == first
    assert len(history) == 3
    assert history[0]["knob_configuration"] == {"random_page_cost": "3"}
    assert history[0]["cost_variant"] == "complete"
    assert history[0]["predicted_operational_cost"] == {"total": 1.0}
    control_campaign(settings, campaign_id, "stop", "design persistence verified", "pytest")
