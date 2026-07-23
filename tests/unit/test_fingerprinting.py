import uuid

from charmdb.fingerprinting import (
    build_fingerprint,
    centroid,
    reference_threshold,
    weighted_distance,
)


def _row(query: str, queryid: int, calls: int, wal: int = 0) -> dict[str, object]:
    return {
        "query": query,
        "queryid": queryid,
        "calls": calls,
        "total_exec_time": calls * 0.5,
        "rows": calls,
        "shared_blks_hit": calls * 2,
        "shared_blks_read": 0,
        "wal_bytes": wal,
    }


def test_fingerprint_operation_ratios_and_templates() -> None:
    fingerprint = build_fingerprint(
        uuid.uuid4(), 0, [_row("SELECT 1", 1, 75), _row("UPDATE x SET y=1", 2, 25, 1000)], 10
    )
    assert fingerprint.raw_features["select_ratio"] == 0.75
    assert fingerprint.raw_features["update_ratio"] == 0.25
    assert sum(fingerprint.template_distribution.values()) == 1


def test_distance_threshold_separates_operation_shift() -> None:
    context = uuid.uuid4()
    reads = [build_fingerprint(context, i, [_row("SELECT 1", 1, 100 + i)], 1) for i in range(5)]
    center = centroid(reads)
    distances = [weighted_distance(item.vector, center) for item in reads]
    threshold = reference_threshold(distances)
    write = build_fingerprint(context, 9, [_row("UPDATE x SET y=1", 2, 100, 5000)], 1)
    assert weighted_distance(write.vector, center) > threshold
