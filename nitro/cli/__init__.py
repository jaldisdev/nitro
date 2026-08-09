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

import click

from nitro import __version__

logger = logging.getLogger("nitro.cli")

BUILTIN_COMMAND_PACKAGE = "nitro.cli.commands"

# Both spellings open the help, which means a command cannot use -h for
# anything else; -H is the convention here for a host option.
_CONTEXT_SETTINGS: dict[str, list[str]] = {"help_option_names": ["-h", "--help"]}


@click.group(context_settings=_CONTEXT_SETTINGS)
@click.version_option(__version__, "-V", "--version", prog_name="nitro")
def cli() -> None:
    """Manage and run Nitro projects."""


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
