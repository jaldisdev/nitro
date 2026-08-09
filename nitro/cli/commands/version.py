"""``nitro version`` — report the installed version."""

from __future__ import annotations

import sys

import click

from nitro import __version__


@click.command("version")
def version() -> None:
    """Show the Nitro version."""
    click.echo(f"Nitro {__version__}")
    click.echo(f"Python {sys.version.split()[0]}")
