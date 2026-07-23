import pytest

from charmdb.reference_block import run_f3_reference_block


def test_f3_reference_block_rejects_fewer_than_five_repetitions() -> None:
    with pytest.raises(ValueError, match="at least five"):
        run_f3_reference_block(object(), object(), repetitions=4)  # type: ignore[arg-type]
