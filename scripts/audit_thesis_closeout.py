"""Verify the frozen D071 evidence inventory without connecting to either database.

Run from the repository with the locked environment. Source evidence is read-only;
the audit and a regenerated final report are written beneath --output.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from charmdb.v2.primary_reporting import render_primary_report

MANIFEST_SHA = "8fcfee8eca196a966f86d16ab6e2b7ea91b3f4bfd401ac360dd7e11b7c807e96"
ANALYSIS_SHA = "769f5a0eabdebe272a0a8ae4b7578fd54f72d4a95cd53822e1cb993466a36fb2"
REPORT_SHA = "97a37d469ca515a4fb4889f6548c8c42ff33b85b1a41d6504dc2e4b551747fd2"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def contained(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    require(path.is_relative_to(root), f"Evidence path escapes root: {relative}")
    return path


def safety_correction(
    runs: list[dict[str, Any]], attempts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep physical attempts distinct from shared-initialization method attribution."""
    by_run = {row["primary_run_id"]: row for row in runs}
    groups: dict[str, set[str]] = {"ALL_PHYSICAL": set(by_run)}
    for run_id, row in by_run.items():
        methods = row["shared_with_methods"] or [row["method"]]
        for method in methods:
            groups.setdefault(method, set()).add(run_id)
    result = []
    for method, run_ids in sorted(groups.items()):
        selected = [a for a in attempts if a["primary_run_id"] in run_ids]
        counts = Counter(a["primary_run_id"] for a in selected)
        failed = [a for a in selected if a["status"] != "COMPLETED"]
        result.append(
            {
                "scope": method,
                "slots": len(run_ids),
                "total_attempts_including_first": len(selected),
                "completed_attempts": sum(a["status"] == "COMPLETED" for a in selected),
                "retained_failed_attempts": len(failed),
                "additional_attempts": sum(max(n - 1, 0) for n in counts.values()),
                "slots_retried": sum(n > 1 for n in counts.values()),
                "attribution": "physical"
                if method == "ALL_PHYSICAL"
                else "shared BO rows attributed to each BO method; do not sum methods",
            }
        )
    return result


def audit(root: Path, output: Path) -> dict[str, Any]:
    root, output = root.resolve(), output.resolve()
    require(not output.is_relative_to(root), "Audit output must be outside source evidence")
    manifest_path = root / "evidence-sha256-manifest.json"
    require(sha256(manifest_path) == MANIFEST_SHA, "D071 manifest trust-anchor mismatch")
    manifest = read_json(manifest_path)
    files = manifest["files"]
    inventory = {item["relative_path"]: item for item in files}
    require(len(inventory) == len(files) == 5457, "Unexpected or duplicate inventory rows")
    total = 0
    for number, item in enumerate(files, 1):
        path = contained(root, item["relative_path"])
        require(path.stat().st_size == item["byte_size"], f"Byte-size mismatch: {path}")
        require(sha256(path) == item["sha256"], f"SHA-256 mismatch: {path}")
        total += item["byte_size"]
        if number % 500 == 0:
            print(f"Verified {number}/{len(files)} source files", flush=True)
    require(total == manifest["total_bytes"] == 60121496447, "Inventory total mismatch")
    extra = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p != manifest_path and p.relative_to(root).as_posix() not in inventory
    )
    require(not extra, f"Unmanifested evidence files: {extra}")

    def rows(name: str) -> list[dict[str, Any]]:
        return list(read_json(root / "evidence-ledgers" / f"{name}.json")["rows"])

    runs, attempts = rows("primary-runs"), rows("primary-attempts")
    require(
        len(runs) == 655 and all(r["status"] == "COMPLETED" for r in runs),
        "Primary completion mismatch",
    )
    require(len({r["primary_run_id"] for r in runs}) == 655, "Duplicate primary IDs")
    require(len(attempts) == 657, "Primary attempt count mismatch")
    require(
        sum(a["status"] == "COMPLETED" for a in attempts) == 655,
        "Primary attempt completion mismatch",
    )
    require(
        sum(r["evaluation_role"] == "DEFAULT_CONTROL" for r in runs) == 25, "Control count mismatch"
    )
    require(
        len(rows("f4-runs")) == 20 and all(r["status"] == "COMPLETED" for r in rows("f4-runs")),
        "F4 completion mismatch",
    )
    phase_b = rows("multifidelity-phase-b-runs")
    require(len(phase_b) == 35, "Phase B count mismatch")
    deployment = rows("apply-best-deployments")
    require(len(deployment) == 1 and deployment[0]["status"] == "ACTIVE", "Deployment mismatch")
    require(deployment[0]["authorization_decision_id"] == "D071", "Authorization mismatch")

    exported = read_json(root / "primary-wave-b/final-five-seed-analysis.json")
    analysis = exported["analysis"]
    payload = {k: v for k, v in analysis.items() if k != "analysis_sha256"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    require(
        hashlib.sha256(encoded).hexdigest() == ANALYSIS_SHA == exported["analysis_sha256"],
        "Analysis hash mismatch",
    )
    require(analysis["independent_seed_count"] == 5, "Independent unit count mismatch")
    source_report = root / "primary-wave-b/report-final-five-seed"
    index_path = source_report / "final-five-seed-report-index.json"
    require(sha256(index_path) == REPORT_SHA, "Report index trust-anchor mismatch")
    index = read_json(index_path)
    output.mkdir(parents=True, exist_ok=True)
    regenerated = output / "report-final-five-seed"
    # Reproduce the frozen historical bytes, including the superseded color cycle.
    result = render_primary_report(
        analysis, index["context"], regenerated, legacy_drift_colors=True
    )
    require(result["index_file_sha256"] == REPORT_SHA, "Regenerated index differs")
    for entry in index["files"]:
        path = contained(regenerated.resolve(), entry["path"])
        require(sha256(path) == entry["sha256"], f"Regenerated report differs: {path}")
        if path.suffix == ".svg":
            ElementTree.parse(path)
    require(
        len(index["files"]) == 16 and len(index["figures"]) == 6, "Final package count mismatch"
    )
    require(
        len(index["table_row_counts"]) == 9 and all(index["table_row_counts"].values()),
        "Empty final table",
    )
    correction = safety_correction(runs, attempts)
    physical = next(row for row in correction if row["scope"] == "ALL_PHYSICAL")
    require(
        physical["additional_attempts"] == physical["slots_retried"] == 2, "Retry count mismatch"
    )
    with (output / "safety-accounting-correction.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=list(correction[0]))
        writer.writeheader()
        writer.writerows(correction)
    (output / "safety-accounting-erratum.md").write_text(
        "# D072 safety-accounting correction\n\n"
        "The frozen final report is retained for provenance. Its safety table incorrectly "
        "interprets the stored total attempt count as a retry count (>0 instead of >1). "
        "Consequently every slot is labelled retried. The safety figure also sums logical "
        "method attributions of shared BO initialization and calls all attempts non-training "
        "infrastructure attempts. Do not reproduce that table's retry column or that figure's "
        "caption in the thesis.\n\n"
        "Use safety-accounting-correction.csv: 655 physical slots, 657 total attempts, "
        "655 completed attempts, two retained failed attempts, and two retried slots. "
        "The failed attempts are DATASET_RESTORE_FAILED and HOST_POWER_INTERRUPTION. "
        "Both concern shared BO initialization; each BO method therefore inherits two "
        "retry attributions, but summing these to six would triple-count physical events. "
        "Random, Sobol, and default controls have zero retries.\n\n"
        "Replacement safety-figure caption: All 655 physical observations completed validly. "
        "Two pre-completion infrastructure failures were retained and retried on the same "
        "candidates without consuming extra candidate slots. Shared BO initialization is "
        "counted once in physical totals.\n\n"
        "This is a post-result reporting correction derived from authenticated attempt rows. "
        "No observation, endpoint estimate, permutation test, method ranking, source analysis "
        "hash, or deployment authorization is changed. The original renderer remains "
        "reproducible; this companion is required when citing its safety accounting.\n",
        encoding="utf-8",
    )
    return {
        "decision": "D072",
        "audited_at_utc": datetime.now(UTC).isoformat(),
        "status": "PASS",
        "scope": (
            "Offline integrity, ledger accounting, and deterministic report reproduction; "
            "not live target or clean-machine reproduction"
        ),
        "source_root": str(root),
        "manifest_file_sha256": MANIFEST_SHA,
        "verified_files": len(files),
        "verified_bytes": total,
        "extra_files": extra,
        "primary_runs": len(runs),
        "primary_attempt_statuses": dict(Counter(a["status"] for a in attempts)),
        "primary_failure_types": dict(
            Counter(a["failure_type"] for a in attempts if a["failure_type"])
        ),
        "physical_controls": 25,
        "physical_candidates": 630,
        "logical_method_slots": 750,
        "independent_seeds": analysis["analysis_seeds"],
        "f4_runs": 20,
        "phase_b_statuses": dict(Counter(r["status"] for r in phase_b)),
        "phase_b_promotions": dict(Counter(str(r["promoted"]) for r in phase_b)),
        "restore_soak_statuses": dict(Counter(r["status"] for r in rows("primary-restore-soaks"))),
        "deployment_state_in_export": deployment[0]["status"],
        "analysis_payload_sha256": ANALYSIS_SHA,
        "report_index_file_sha256": REPORT_SHA,
        "report_rerender_byte_identical": True,
        "publication_status": "USE_WITH_D072_SAFETY_ERRATUM",
        "safety_accounting_correction": correction,
        "table_row_counts": index["table_row_counts"],
        "valid_svg_count": 6,
        "candidate_reliability": analysis["candidate_reliability"],
        "method_results": analysis["method_results"],
        "reproduction_output": str(regenerated),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.root, args.output)
    destination = args.output / "audit.json"
    destination.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"PASS: {destination}", flush=True)


if __name__ == "__main__":
    main()
