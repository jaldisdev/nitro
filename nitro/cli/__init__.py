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

"""Command line entry point and command discovery.

Commands are Click commands found by walking a package: every module in it is
imported and any ``click.Command`` it defines at module level is registered.
Built-in commands live in ``nitro.cli.commands``; a project adds its own by
listing packages in the ``COMMAND_MODULES`` setting.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import sys
from pathlib import Path
from typing import Any

import click

from nitro import __version__
from nitro.cli.serve import serve

logger = logging.getLogger("nitro.cli")

BUILTIN_COMMAND_PACKAGE = "nitro.cli.commands"


class RootCommand(click.Group):
    """The root command: serves an application, and hosts the other commands.

    Serving is what the command line is mostly for, so it is what `nitro` does
    with no command word in front of it — `nitro app:app`, not
    `nitro run app:app`. Anything that is not one of the named commands is
    taken as an application specifier and handed to the server.
    """

    #: What runs when no named command is given. Deliberately not registered
    #: as a command, so there is no second spelling for it.
    serving: click.Command = serve

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Both spellings open the help, which means a command cannot use -h for
        # anything else; -H is the convention here for a host option.
        #
        # Unknown options are passed through rather than rejected: the root
        # command's own options are the serving ones, and they are parsed by
        # the command the arguments are handed to.
        settings: dict[str, Any] = {
            "help_option_names": ["-h", "--help"],
            "ignore_unknown_options": True,
        }
        settings.update(kwargs.pop("context_settings", None) or {})
        super().__init__(*args, context_settings=settings, **kwargs)

    def resolve_command(
        self, context: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args and self.get_command(context, args[0]) is None:
            # A bare word can only have been meant as a command, since an
            # application always carries a colon. Saying so beats letting it
            # through to be rejected as a malformed specifier.
            first = args[0]
            if not first.startswith("-") and ":" not in first:
                commands = ", ".join(sorted(self.list_commands(context)))
                raise click.UsageError(
                    f"{first!r} is not a command. An application is served directly, "
                    f"as '{context.command_path} module:attribute'. Commands: {commands}.",
                    ctx=context,
                )
            # A name of `None` keeps the sub-context out of the usage line, so
            # errors read `Usage: nitro ...` rather than naming a command that
            # cannot be typed. The arguments are passed on whole: the first is
            # the application, not a command word to strip.
            return None, self.serving, args
        return super().resolve_command(context, args)

    def format_usage(self, context: click.Context, formatter: click.HelpFormatter) -> None:
        pieces = " ".join(self.serving.collect_usage_pieces(context))
        formatter.write_usage(context.command_path, pieces)
        formatter.write_usage(context.command_path, "COMMAND [ARGS]...", prefix="   or: ")

    def format_options(self, context: click.Context, formatter: click.HelpFormatter) -> None:
        # The serving options are the root command's own, even though they are
        # declared on the command that implements it.
        records = [
            record
            for parameter in (*self.serving.params, *self.get_params(context))
            if (record := parameter.get_help_record(context)) is not None
        ]
        if records:
            with formatter.section("Options"):
                formatter.write_dl(records)
        self.format_commands(context, formatter)


@click.group(cls=RootCommand)
@click.version_option(__version__, "-V", "--version", prog_name="nitro")
def cli() -> None:
    """Serve APPLICATION, given as 'module:attribute'.

    Manage and run Nitro projects. With no command word, the argument is an
    application to serve.
    """


def commands_in(package_name: str) -> list[click.Command]:
    """Every Click command defined by the modules of `package_name`."""
    try:
        package = importlib.import_module(package_name)
    except ImportError as error:
        raise ImportError(f"Could not import command package {package_name!r}: {error}") from error

    search_paths = getattr(package, "__path__", None)
    if search_paths is None:
        return _commands_in_module(package)

    found: list[click.Command] = []
    for module_info in pkgutil.iter_modules(search_paths):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package_name}.{module_info.name}")
        found.extend(_commands_in_module(module))
    return found


def _commands_in_module(module: object) -> list[click.Command]:
    return [
        value
        for name, value in vars(module).items()
        if not name.startswith("_") and isinstance(value, click.Command)
    ]


def register_commands(group: click.Group, package_name: str) -> None:
    for command in commands_in(package_name):
        group.add_command(command)


def load_project_commands(group: click.Group) -> None:
    """Register commands from the project's ``COMMAND_MODULES`` setting.

    A project that is not configured, or whose settings cannot be read, still
    gets the built-in commands — otherwise a broken settings module would leave
    no way to run anything at all.
    """
    from nitro.settings import settings

    try:
        packages = list(settings.COMMAND_MODULES)
    except Exception:
        logger.exception("could not read COMMAND_MODULES; only built-in commands are available")
        return

    for package_name in packages:
        try:
            register_commands(group, package_name)
        except ImportError:
            logger.exception("skipping command package %r", package_name)


def main() -> None:
    # A project's own modules live in the working directory, which is not on
    # the path when the console script runs from an installed package.
    working_directory = str(Path.cwd())
    if working_directory not in sys.path:
        sys.path.insert(0, working_directory)

    register_commands(cli, BUILTIN_COMMAND_PACKAGE)
    load_project_commands(cli)
    cli()
