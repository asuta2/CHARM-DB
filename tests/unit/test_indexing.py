import pytest

from charmdb.indexing import QueryEvidence, generate_candidates, make_candidate


def test_candidate_normalization_orders_keys_and_bounds_include() -> None:
    candidate = make_candidate(
        QueryEvidence(
            "public",
            "orders",
            ("customer_id",),
            range_columns=("amount",),
            order_columns=("created_at",),
            projection_columns=("id", "status", "payload"),
            frequency=20,
        )
    )
    assert candidate.key_columns == ("customer_id", "amount", "created_at")
    assert candidate.include_columns == ("id", "status")
    assert candidate.index_name.startswith("charm_idx_")
    assert "INCLUDE (id, status)" in candidate.normalized_sql


def test_candidate_is_deterministic_and_deduplicated() -> None:
    evidence = QueryEvidence("public", "orders", ("customer_id",), frequency=5)
    first = make_candidate(evidence)
    second = make_candidate(evidence)
    assert first == second
    assert generate_candidates([evidence, evidence]) == [first]


def test_unsafe_identifier_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsafe"):
        make_candidate(QueryEvidence("public", "orders;drop table x", ("customer_id",)))
