from pathlib import Path

from charmdb.protocol import FINAL_SCREENING_PARAMETERS, load_manifest

MANIFEST = Path("experiments/thesis/manifests/f4-confirmation.json")


def test_f4_design_is_ready_only_after_p010_and_implementation() -> None:
    manifest = load_manifest(MANIFEST)

    assert manifest.stage == "f4-confirmation"
    assert manifest.evidence_role == "F4_CONFIRMATION"
    assert manifest.status == "ready"
    assert manifest.payload["execution_ready"] is True
    assert manifest.payload["prerequisites"]["p010_supervisor_confirmation_recorded"] is True
    assert manifest.payload["prerequisites"]["durable_f4_runner_implemented"] is True
    assert manifest.payload["prerequisites"]["result_blind_f4_analysis_implemented"] is True
    assert not manifest.unresolved_decisions


def test_f4_recommendation_is_a_complete_balanced_twenty_observation_design() -> None:
    payload = load_manifest(MANIFEST).payload
    design = payload["recommended_design"]
    finalists = payload["finalists"]
    schedule = design["schedule"]

    assert design["physical_observations"] == 20
    assert design["blocks"] == 4
    assert design["observations_per_block"] == 5
    assert design["candidate_repetitions"] == 4
    assert design["default_repetitions"] == 4
    assert len(finalists) == 4
    assert len(schedule) == 4

    labels = {candidate["label"] for candidate in finalists}
    assert labels == {"T", "L", "H", "E"}
    assert len({candidate["primary_run_id"] for candidate in finalists}) == 4
    assert len(
        {
            tuple(sorted(candidate["requested_configuration"].items()))
            for candidate in finalists
        }
    ) == 4
    assert all(
        set(candidate["requested_configuration"]) == set(FINAL_SCREENING_PARAMETERS)
        for candidate in finalists
    )

    reserved_wave_b_seeds = {1902413987, 740267717}
    block_seeds = {block["seed"] for block in schedule}
    assert len(block_seeds) == 4
    assert not block_seeds & reserved_wave_b_seeds
    for block in schedule:
        assert len(block["order"]) == 5
        assert block["order"][2] == "DEFAULT"
        assert set(block["order"]) == labels | {"DEFAULT"}

    for label in labels:
        candidate_positions = [block["order"].index(label) for block in schedule]
        assert sorted(candidate_positions) == [0, 1, 3, 4]


def test_f4_selection_rule_cannot_authorize_deployment() -> None:
    payload = load_manifest(MANIFEST).payload

    assert payload["recommended_selection_rule"]["selection_is_not_apply_best_authorization"]
    assert payload["recommended_selection_rule"]["no_eligible_candidate"].startswith(
        "retain-postgresql-default"
    )
    assert payload["separate_decisions"]["wave_b"].startswith("unauthorized")
    assert payload["separate_decisions"]["apply_best"].startswith("unauthorized")
