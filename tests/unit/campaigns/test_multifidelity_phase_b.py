from charmdb.campaigns.multifidelity import (
    INDEX_FIXTURE_SEQUENCE_MISMATCH_KEYS,
    _fixture_remediation_blockers,
    analyze_phase_b_observations,
    build_phase_b_plan,
    phase_b_manifest_payload,
)


def test_phase_b_plan_is_frozen_and_causally_blocked() -> None:
    _, payload = phase_b_manifest_payload()
    plan = build_phase_b_plan()

    assert len(plan) == 35
    assert [slot.physical_position for slot in plan if slot.slot_kind == "DEFAULT_CONTROL"] == [
        1,
        8,
        15,
        22,
        29,
    ]
    assert [slot.candidate_position for slot in plan if slot.slot_kind == "CANDIDATE"] == list(
        range(1, 31)
    )
    assert len({slot.random_seed for slot in plan}) == 35
    assert (
        len({slot.continuation_seed for slot in plan if slot.continuation_seed is not None}) == 30
    )
    assert payload["phase_b"]["candidate_design_sha256"] == (
        "e253fde5686b9ac52f42105d053ff0fd332844a4298eea5f12dc6f22dd242812"
    )


def test_phase_b_does_not_consume_reserved_wave_b_seeds() -> None:
    _, payload = phase_b_manifest_payload()
    plan = build_phase_b_plan()
    seeds = {slot.random_seed for slot in plan} | {
        slot.continuation_seed for slot in plan if slot.continuation_seed is not None
    }
    assert payload["phase_b"]["reserved_wave_b_seeds_consumed"] is False
    assert seeds.isdisjoint({1902413987, 740267717})


def test_fixture_remediation_is_allowed_only_for_the_exact_failed_gate() -> None:
    campaign = {"campaign_status": "PAUSED", "block_status": "RUNNING"}
    attempt = {
        "physical_position": 1,
        "attempt_number": 1,
        "run_status": "CREATED",
        "attempt_status": "CREATED",
        "trial_state": "DATASET_RESTORE_FAILED",
        "failure_type": "DATASET_RESTORE_FAILED",
        "exact_core_passed": False,
        "physical_statistics_passed": True,
        "verification_details": {
            "exact_mismatches": {key: {} for key in INDEX_FIXTURE_SEQUENCE_MISMATCH_KEYS},
            "physical_mismatches": {},
        },
    }
    target = {"recognized_test_fixture": True, "active_charm_sessions": 0}

    assert _fixture_remediation_blockers(campaign, attempt, target) == []

    unexpected = {**attempt, "verification_details": dict(attempt["verification_details"])}
    unexpected["verification_details"] = {
        **unexpected["verification_details"],
        "exact_mismatches": {
            **unexpected["verification_details"]["exact_mismatches"],
            "relation_rows.public.pgbench_history": {},
        },
    }
    assert "restore-has-mismatches-beyond-the-known-test-sequence" in (
        _fixture_remediation_blockers(campaign, unexpected, target)
    )


def test_fixture_remediation_rejects_unrecognized_or_active_targets() -> None:
    campaign = {"campaign_status": "RUNNING", "block_status": "RUNNING"}
    attempt = {
        "physical_position": 2,
        "attempt_number": 2,
        "run_status": "RETRY_PENDING",
        "attempt_status": "INFRASTRUCTURE_FAILED",
        "trial_state": "DATASET_RESTORE_FAILED",
        "failure_type": "DATASET_RESTORE_FAILED",
        "exact_core_passed": False,
        "physical_statistics_passed": True,
        "verification_details": {
            "exact_mismatches": {key: {} for key in INDEX_FIXTURE_SEQUENCE_MISMATCH_KEYS},
            "physical_mismatches": {},
        },
    }
    blockers = _fixture_remediation_blockers(
        campaign,
        attempt,
        {"recognized_test_fixture": False, "active_charm_sessions": 1},
    )

    assert "campaign-not-paused" in blockers
    assert "failure-is-not-first-slot-attempt-one" in blockers
    assert "target-object-is-not-the-exact-known-test-fixture" in blockers
    assert "target-has-active-charm-sessions" in blockers


def test_phase_b_terminal_analysis_applies_the_frozen_success_rule() -> None:
    _, payload = phase_b_manifest_payload()
    plan = build_phase_b_plan()
    successful_seconds = (15.453 * 3600.0 - 540.0) / 35.0
    rows = []
    for slot in plan:
        rejected = slot.candidate_position == 26
        promoted = None if slot.slot_kind == "DEFAULT_CONTROL" else not rejected
        rows.append(
            {
                "slot_kind": slot.slot_kind,
                "physical_position": slot.physical_position,
                "candidate_position": slot.candidate_position,
                "promoted": promoted,
                "f2_throughput_tps": 790.0 if rejected else 900.0,
                "promotion_baseline_tps": 1000.0,
                "reconnect_gap_seconds": 0.01 if promoted else None,
                "infrastructure_attempts": 2 if slot.physical_position == 1 else 1,
                "lifecycle_seconds": successful_seconds,
                "exact_core_passed": True,
                "physical_statistics_passed": True,
                "authenticated": True,
                "constraint_values": {"failures": 0},
                "objective_values": {"throughput_tps": 1000.0, "p99_ms": 30.0},
                "workflow_result": {
                    "duration_seconds": 600.0 if promoted else 60.0,
                },
            }
        )
    failed = [{"lifecycle_seconds": 180.0}]

    result = analyze_phase_b_observations(rows, failed, payload)

    assert result["decision"] == "LIVE_DEMONSTRATION_PASSED"
    assert result["counts"]["promoted"] == 29
    assert result["counts"]["rejected"] == 1
    assert result["time"]["saved_seconds"] == 540.0
    assert result["success_gates"]["absolute_time_model_error_relative"] == 0.0
    assert result["rejected_candidates"][0]["candidate_position"] == 26
