"""CHARM-DB canonical CLI entry point."""

from charmdb.cli_commands import app
from charmdb.cli_commands import calibration as calibration
from charmdb.cli_commands import confirmation as confirmation
from charmdb.cli_commands import evidence as evidence
from charmdb.cli_commands import multifidelity as multifidelity
from charmdb.cli_commands import primary as primary
from charmdb.cli_commands import runtime as runtime

__all__ = ["app"]

if __name__ == "__main__":
    app()
