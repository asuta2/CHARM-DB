import uuid

from charmdb.experiment_execution import validate_execution_definition
from charmdb.manifest_builder import build_search_execution_definition


def test_generated_search_definition_is_complete_and_uses_measured_reference() -> None:
    preflight_id = uuid.uuid4()
    block_id = uuid.uuid4()
    evidence = {
        "dataset": {
            "snapshot_sha256": "a" * 64,
            "schema_sha256": "b" * 64,
            "content_sha256": "c" * 64,
            "snapshot_relative_path": "dataset-snapshots/test.dump",
            "relations": ["public.pgbench_accounts"],
            "relation_rows": {"public.pgbench_accounts": 1_000_000},
            "scale": 10,
        },
        "proposed_execution_profile": {
            "warmup_seconds": 120,
            "measurement_seconds": 600,
            "concurrency": 4,
        },
        "resources": {
            "container": {
                "cpu_limit_nanos": 4_000_000_000,
                "memory_limit_bytes": 4_294_967_296,
                "docker_host_memory_bytes": 8_282_886_144,
                "pids_limit": 512,
            }
        },
        "target_safety": {"version": "PostgreSQL 18.1"},
        "software_versions": {"charmdb": "0.1.0", "python": "3.12"},
    }

    definition = build_search_execution_definition(preflight_id, evidence, block_id, 729.484752)

    validate_execution_definition(definition, 20.0)
    assert definition["budget_accounting"]["f3_reference_wall_clock_seconds"] == 729.484752
    assert definition["dataset"]["content_sha256"] == "c" * 64
