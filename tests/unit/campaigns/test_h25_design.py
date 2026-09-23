from pathlib import Path

from charmdb.protocol import FINAL_SCREENING_PARAMETERS, load_manifest

MANIFEST = Path("experiments/thesis/manifests/h25-heuristic-baseline.json")
F4_MANIFEST = Path("experiments/thesis/manifests/f4-confirmation.json")
PRIMARY_MANIFEST = Path("experiments/thesis/manifests/primary-comparison.json")


def test_h25_manifest_is_a_ready_secondary_supplement() -> None:
    manifest = load_manifest(MANIFEST)

    assert manifest.stage == "h25-heuristic-baseline"
    assert manifest.evidence_role == "SECONDARY"
    assert manifest.status == "ready"
    assert manifest.payload["execution_ready"] is True
    assert all(manifest.payload["prerequisites"].values())
    assert not manifest.unresolved_decisions
    assert manifest.payload["question"]["changes_champion_selection"] is False
    assert manifest.payload["question"]["changes_apply_best_state"] is False
    assert (
        "ACTIVE"
        in manifest.payload["live_target_precondition"]["apply_best_deployment_status_must_not_be"]
    )


def test_h25_schedule_reuses_f4_seeds_with_default_at_center_and_balanced_outer_arms() -> None:
    payload = load_manifest(MANIFEST).payload
    f4_schedule = load_manifest(F4_MANIFEST).payload["recommended_design"]["schedule"]
    design = payload["design"]
    schedule = design["schedule"]

    assert design["design_kind"] == "three-arm-heuristic-default-champion"
    assert design["physical_observations"] == 12
    assert design["blocks"] == 4
    assert design["observations_per_block"] == 3
    assert design["default_at_center_position"] is True
    assert len(schedule) == 4
    assert [block["seed"] for block in schedule] == [block["seed"] for block in f4_schedule]
    assert [block["order"] for block in schedule] == [
        ["H25", "DEFAULT", "E"],
        ["E", "DEFAULT", "H25"],
        ["E", "DEFAULT", "H25"],
        ["H25", "DEFAULT", "E"],
    ]
    for block in schedule:
        assert block["order"][1] == "DEFAULT"
        assert sorted(block["order"]) == ["DEFAULT", "E", "H25"]
    for label in ("H25", "E"):
        assert sorted(block["order"].index(label) for block in schedule) == [0, 0, 2, 2]


def test_h25_treatment_changes_only_shared_buffers_within_bounds() -> None:
    payload = load_manifest(MANIFEST).payload
    default = payload["treatments"]["DEFAULT"]["requested_configuration"]
    heuristic = payload["treatments"]["H25"]["requested_configuration"]
    frozen_default = load_manifest(PRIMARY_MANIFEST).payload["postgresql_default_configuration"]

    assert default == frozen_default
    assert set(default) == set(FINAL_SCREENING_PARAMETERS)
    assert {name for name in default if default[name] != heuristic[name]} == {"shared_buffers"}
    assert heuristic["shared_buffers"] == "131072"
    lower, upper = payload["treatments"]["H25"]["within_frozen_bounds"]
    assert lower <= int(heuristic["shared_buffers"]) <= upper
    assert payload["treatments"]["H25"]["changed_value_bytes"] == 131072 * 8192
    assert payload["source_evidence"]["f4_context_is_contemporaneous"] is False


def test_h25_champion_arm_is_the_frozen_f4_finalist_e() -> None:
    payload = load_manifest(MANIFEST).payload
    finalists = {row["label"]: row for row in load_manifest(F4_MANIFEST).payload["finalists"]}
    champion = payload["treatments"]["E"]

    assert champion["f4_label"] == "E"
    assert champion["source_primary_run_id"] == finalists["E"]["primary_run_id"]
    assert champion["requested_configuration"] == finalists["E"]["requested_configuration"]
    assert (
        payload["source_evidence"]["champion_source_primary_run_id"]
        == finalists["E"]["primary_run_id"]
    )
