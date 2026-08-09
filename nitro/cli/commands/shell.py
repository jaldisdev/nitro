"""``nitro shell`` — an interactive shell with the project loaded."""

from __future__ import annotations

import code
import sys
from typing import Any

import click


def build_namespace() -> dict[str, Any]:
    """What the shell starts with in scope."""
    import nitro
    from nitro.settings import settings

    namespace: dict[str, Any] = {"__name__": "__main__", "nitro": nitro, "settings": settings}

    # A project's application is the thing most sessions start from, so it is
    # offered when there is an obvious one to offer.
    try:
        from nitro.routing.reverse import active_router

        namespace["router"] = active_router()
    except LookupError:
        pass

    return namespace


@click.command("shell")
@click.option(
    "-i",
    "--interface",
    type=click.Choice(["auto", "python", "ipython", "bpython"]),
    default="auto",
    help="Which shell to open. [default: auto]",
)
def shell(interface: str) -> None:
    """Open an interactive shell."""
    namespace = build_namespace()
    banner = f"Nitro shell — Python {sys.version.split()[0]}"

    if interface in ("auto", "ipython"):
        try:
            from IPython import embed

            embed(user_ns=namespace, banner1=f"{banner}\n")
            return
        except ImportError:
            if interface == "ipython":
                raise click.ClickException("IPython is not installed") from None

    if interface in ("auto", "bpython"):
        try:
            import bpython

            bpython.embed(locals_=namespace, banner=banner)
            return
        except ImportError:
            if interface == "bpython":
                raise click.ClickException("bpython is not installed") from None

    code.interact(local=namespace, banner=banner)
