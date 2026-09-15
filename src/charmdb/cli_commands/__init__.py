"""Canonical CLI command registry."""

import typer

app = typer.Typer(
    no_args_is_help=True,
    help="CHARM-DB research controller",
    pretty_exceptions_show_locals=False,
)
