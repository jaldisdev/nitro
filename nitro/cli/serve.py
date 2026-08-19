#
# This source file is part of the Nitro open source project.
#
# Copyright (c) 2026 Jaldis B.V.
#
# Licensed under the MIT OR Apache-2.0 license (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://opensource.org/licenses/MIT
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""``nitro APPLICATION`` — start the bundled server.

This lives outside ``nitro.cli.commands`` on purpose. Serving is what the root
command does, not a command of its own, and everything in that package is
registered as a subcommand.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import click

from nitro.settings import ImproperlyConfigured, ServerOptions

DEFAULT_APPLICATION = "app:app"


class RootPathContext(click.Context):
    """A context that answers with the path of the command above it.

    Serving is reached without a command word, so its own name must not turn up
    in usage lines or in the "try --help" hint on an error.
    """

    @property
    def command_path(self) -> str:
        if self.parent is not None:
            return self.parent.command_path
        return click.Context.command_path.fget(self)


class ServeCommand(click.Command):
    context_class = RootPathContext


def load_application(specifier: str) -> Any:
    """Import an application from a ``module:attribute`` specifier."""
    module_name, separator, attribute = specifier.partition(":")
    if not separator or not module_name or not attribute:
        # Anything that is not a known command reaches this, so a mistyped
        # command word lands here too and the message has to make sense for it.
        raise click.BadParameter(
            f"expected an application as 'module:attribute', got {specifier!r}",
            param_hint="APPLICATION",
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


@click.command("serve", cls=ServeCommand)
@click.argument("application", default=DEFAULT_APPLICATION)
@click.option("-H", "--host", help="Interface to bind. [default: localhost]")
@click.option("-p", "--port", type=int, help="Port to bind. [default: 8000]")
@click.option("--uds", type=click.Path(), help="Bind a Unix domain socket instead of a port.")
@click.option("-w", "--workers", type=int, help="Number of worker processes. [default: 1]")
@click.option("--runtime-threads", type=int, help="Threads per worker's runtime. [default: 2]")
@click.option(
    "--http",
    type=click.Choice(["auto", "1", "2", "3"]),
    help="Highest HTTP version to negotiate. [default: auto]",
)
@click.option("--tls-cert", type=click.Path(exists=True, dir_okay=False), help="TLS certificate.")
@click.option("--tls-key", type=click.Path(exists=True, dir_okay=False), help="TLS private key.")
@click.option("--access-log/--no-access-log", default=None, help="Write an access log.")
@click.option(
    "--reload",
    is_flag=True,
    default=False,
    help="Restart when a Python file changes. Development only.",
)
@click.option(
    "-l",
    "--log-level",
    type=click.Choice(["trace", "debug", "info", "warning", "error"]),
    help="Server log level. [default: info]",
)
def serve(application: str, reload: bool, **overrides: Any) -> None:
    """Serve APPLICATION, given as 'module:attribute'."""
    from nitro.app import build_server, served_addresses

    if reload:
        # Imported here, and only here, so serving normally never loads the
        # supervisor. The check comes before the application does: the parent
        # supervises, and only the child pays to import the project.
        from nitro.reload import is_reload_child, run_with_reloader

        if not is_reload_child():
            raise SystemExit(run_with_reloader())

    loaded = load_application(application)
    if callable(loaded) and not hasattr(loaded, "__handle_http__"):
        loaded = loaded()

    try:
        server, options = build_server(loaded, **overrides)
    except ImproperlyConfigured as error:
        raise click.ClickException(str(error)) from error
    except (ValueError, RuntimeError) as error:
        raise click.ClickException(str(error)) from error

    for address in served_addresses(server, options):
        click.echo(f"Serving on {address}")
    if options.uds:
        click.echo(f"Serving on {options.uds}")

    server.serve()
