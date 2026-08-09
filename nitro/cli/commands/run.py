"""``nitro run`` — start the bundled server."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import click

from nitro.settings import ImproperlyConfigured, ServerOptions

DEFAULT_APPLICATION = "app:app"


def load_application(specifier: str) -> Any:
    """Import an application from a ``module:attribute`` specifier."""
    module_name, separator, attribute = specifier.partition(":")
    if not separator or not module_name or not attribute:
        raise click.BadParameter(
            f"expected 'module:attribute', got {specifier!r}", param_hint="APPLICATION"
        )

    # The working directory is where a project's own modules live, and it is
    # not on the path when the console script runs from an installed package.
    working_directory = str(Path.cwd())
    if working_directory not in sys.path:
        sys.path.insert(0, working_directory)

    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise click.ClickException(
            f"Could not import {module_name!r} from {working_directory}: {error}"
        ) from error

    try:
        return getattr(module, attribute)
    except AttributeError as error:
        raise click.ClickException(
            f"Module {module_name!r} has no attribute {attribute!r}"
        ) from error


def build_options(application: Any, **overrides: Any) -> ServerOptions:
    """Resolve server options, letting the application contribute its own."""
    resolver = getattr(application, "server_options", None)
    if callable(resolver):
        return resolver(**overrides)
    return ServerOptions.resolve(**overrides)


@click.command("run")
@click.argument("application", default=DEFAULT_APPLICATION)
@click.option("-H", "--host", help="Interface to bind. [default: localhost]")
@click.option("-p", "--port", type=int, help="Port to bind. [default: 8000]")
@click.option("--uds", type=click.Path(), help="Bind a Unix domain socket instead of a port.")
@click.option("-w", "--workers", type=int, help="Number of worker processes. [default: 1]")
@click.option(
    "--runtime-threads", type=int, help="Threads per worker's runtime. [default: 1]"
)
@click.option(
    "--http",
    type=click.Choice(["auto", "1", "2", "3"]),
    help="Highest HTTP version to negotiate. [default: auto]",
)
@click.option("--tls-cert", type=click.Path(exists=True, dir_okay=False), help="TLS certificate.")
@click.option("--tls-key", type=click.Path(exists=True, dir_okay=False), help="TLS private key.")
@click.option("--access-log/--no-access-log", default=None, help="Write an access log.")
@click.option(
    "-l",
    "--log-level",
    type=click.Choice(["trace", "debug", "info", "warning", "error"]),
    help="Server log level. [default: info]",
)
def run(application: str, **overrides: Any) -> None:
    """Serve APPLICATION, given as 'module:attribute'."""
    from nitro._nitro import Server

    loaded = load_application(application)
    if callable(loaded) and not hasattr(loaded, "__handle_http__"):
        loaded = loaded()

    try:
        options = build_options(loaded, **overrides)
    except ImproperlyConfigured as error:
        raise click.ClickException(str(error)) from error

    try:
        server = Server(loaded, options)
    except (ValueError, RuntimeError) as error:
        raise click.ClickException(str(error)) from error

    for host, port in server.addresses:
        click.echo(f"Serving on http://{host}:{port}")
    if options.uds:
        click.echo(f"Serving on {options.uds}")

    server.serve()
