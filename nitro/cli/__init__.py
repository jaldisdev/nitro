"""Command line entry point and command auto-discovery."""

from __future__ import annotations

import click

from nitro import __version__

_CONTEXT_SETTINGS: dict[str, list[str]] = {"help_option_names": ["-h", "--help"]}


@click.group(context_settings=_CONTEXT_SETTINGS)
@click.version_option(__version__, "-V", "--version", prog_name="nitro")
def cli() -> None:
    """Manage and run Nitro projects."""


def main() -> None:
    cli()
