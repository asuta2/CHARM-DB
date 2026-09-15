from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from charmdb.protocol import (
    FINAL_SCREENING_PARAMETERS,
    PRIMARY_SEARCH_METHODS,
    load_manifest,
    validate_manifest,
    validate_manifest_directory,
)

ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = ROOT / "experiments" / "thesis" / "manifests"


class ProtocolManifestTests(unittest.TestCase):
    def test_all_committed_manifests_validate(self) -> None:
        manifests = validate_manifest_directory(CONFIG_DIR)
        self.assertEqual(len(manifests), 15)
        statuses = {item.stage: item.status for item in manifests}
        self.assertEqual(statuses["new-machine-bootstrap"], "ready")
        self.assertEqual(statuses["candidate-restore-pilot"], "ready")
        self.assertEqual(statuses["sanity-and-saturation"], "ready")
        self.assertEqual(statuses["warmup-and-f3-duration-pilot"], "ready")
        self.assertEqual(statuses["new-machine-default-reference"], "ready")
        self.assertEqual(statuses["parameter-screening"], "ready")
        self.assertEqual(statuses["parameter-screening-recovery"], "ready")
        self.assertEqual(statuses["parameter-screening-recovery-remediation"], "ready")
        self.assertEqual(statuses["post-screening-temporal-stability"], "blocked")
        self.assertEqual(statuses["screening-interpretation-amendment"], "ready")
        # D041 recorded P007 and promoted the primary manifest; the validator
        # only accepts that state when every freeze gate and both policy
        # statuses are frozen with no unresolved decision left.
        self.assertEqual(statuses["primary-comparison"], "ready")
        self.assertEqual(statuses["multi-fidelity"], "ready")
        self.assertEqual(statuses["f4-confirmation"], "ready")
        self.assertEqual(statuses["primary-wave-b"], "ready")
        self.assertEqual(statuses["apply-best"], "blocked")
        self.assertTrue(
            all(
                status == "draft"
                for stage, status in statuses.items()
                if stage
                not in {
                    "new-machine-bootstrap",
                    "candidate-restore-pilot",
                    "sanity-and-saturation",
                    "warmup-and-f3-duration-pilot",
                    "new-machine-default-reference",
                    "parameter-screening",
                    "parameter-screening-recovery",
                    "parameter-screening-recovery-remediation",
                    "post-screening-temporal-stability",
                    "screening-interpretation-amendment",
                    "primary-comparison",
                    "multi-fidelity",
                    "f4-confirmation",
                    "primary-wave-b",
                    "apply-best",
                }
            )
        )
        primary = next(item for item in manifests if item.stage == "primary-comparison")
        self.assertEqual(primary.unresolved_decisions, ())

    def test_d052_multifidelity_rule_is_fail_closed(self) -> None:
        payload = json.loads((CONFIG_DIR / "multi-fidelity.json").read_text(encoding="utf-8"))
        invalid = copy.deepcopy(payload)
        invalid["phase_a"]["promotion_rule"]["throughput_floor_ratio"] = 0.79
        with self.assertRaisesRegex(ValueError, "D052"):
            validate_manifest(invalid)
        blocked = copy.deepcopy(payload)
        blocked["prerequisites"]["durable_phase_a_analyzer_implemented"] = False
        with self.assertRaisesRegex(ValueError, "validated deterministic analyzer"):
            validate_manifest(blocked)

    def test_d054_phase_b_contract_is_fail_closed(self) -> None:
        payload = json.loads((CONFIG_DIR / "multi-fidelity.json").read_text(encoding="utf-8"))
        invalid = copy.deepcopy(payload)
        invalid["phase_b"]["benchmark_profile"]["promoted_continuation_seconds"] = 539
        with self.assertRaisesRegex(ValueError, "benchmark profile"):
            validate_manifest(invalid)
        invalid = copy.deepcopy(payload)
        invalid["phase_b"]["durable_runner_implemented"] = False
        with self.assertRaisesRegex(ValueError, "validated durable two-stage runner"):
            validate_manifest(invalid)

    def test_primary_uses_five_methods_and_excludes_default(self) -> None:
        manifest = load_manifest(CONFIG_DIR / "primary-comparison.json")
        self.assertEqual(manifest.payload["search_methods"], list(PRIMARY_SEARCH_METHODS))
        self.assertNotIn("postgresql_default", manifest.payload["search_methods"])

    def test_d035_freezes_eight_knobs_and_30_by_3_wave_a(self) -> None:
        amendment = load_manifest(CONFIG_DIR / "screening-interpretation-amendment.json")
        primary = load_manifest(CONFIG_DIR / "primary-comparison.json")

        self.assertEqual(
            amendment.payload["final_search_space"]["parameter_order"],
            list(FINAL_SCREENING_PARAMETERS),
        )
        self.assertEqual(
            primary.payload["search_space"]["parameter_order"],
            list(FINAL_SCREENING_PARAMETERS),
        )
        self.assertEqual(primary.payload["candidate_budget_per_method"], 30)
        self.assertEqual(primary.payload["wave_a"]["seed_count"], 3)
        self.assertEqual(primary.payload["wave_b"]["status"], "reserved-not-authorized")

    def test_durability_knobs_are_rejected_explicitly(self) -> None:
        payload = json.loads((CONFIG_DIR / "parameter-screening.json").read_text(encoding="utf-8"))
        for knob in ("fsync", "synchronous_commit", "full_page_writes"):
            invalid = copy.deepcopy(payload)
            invalid["search_space"]["parameters"][knob] = {"type": "boolean"}
            with self.subTest(knob=knob), self.assertRaisesRegex(ValueError, "durability knobs"):
                validate_manifest(invalid)

    def test_primary_cannot_be_ready_before_freeze_gates(self) -> None:
        payload = json.loads((CONFIG_DIR / "primary-comparison.json").read_text(encoding="utf-8"))
        payload["status"] = "ready"
        payload["unresolved_decisions"] = []
        payload["prerequisites"]["primary_protocol_frozen"] = False
        with self.assertRaisesRegex(ValueError, "before gates pass"):
            validate_manifest(payload)

    def test_screening_cannot_be_ready_before_runner_gate(self) -> None:
        payload = json.loads((CONFIG_DIR / "parameter-screening.json").read_text(encoding="utf-8"))
        payload["execution_ready"] = False
        payload["prerequisites"]["durable_runner_implemented"] = False
        with self.assertRaisesRegex(ValueError, "before its durable runner"):
            validate_manifest(payload)


if __name__ == "__main__":
    unittest.main()
