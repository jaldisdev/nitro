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

"""``nitro static collect`` — gather the files a deployment serves as they are."""

from __future__ import annotations

import shutil
from pathlib import Path

import click

from nitro.settings import ImproperlyConfigured, settings


@click.group("static")
def static() -> None:
    """Files served as they are."""


def _configured(name: str, default: object = None) -> object:
    try:
        return getattr(settings, name)
    except (AttributeError, ImproperlyConfigured):
        return default


def _sources() -> list[Path]:
    return [Path(directory).resolve() for directory in _configured("STATIC_DIRS", []) or []]


def _files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*") if path.is_file())


@static.command("collect")
@click.option("--clear", is_flag=True, help="Empty the destination before collecting.")
def collect(clear: bool) -> None:
    """Copy every file in STATIC_DIRS into STATIC_ROOT.

    A file is copied when the destination is missing or differs in size or
    modification time, so collecting again over an existing directory does the
    little that changed rather than all of it.

    Where two source directories hold the same relative path, the one named
    first wins — the order in the setting is the order of precedence, so a
    project can override a file it takes from elsewhere by listing its own
    directory first.
    """
    root = _configured("STATIC_ROOT")
    if not root:
        raise click.ClickException("STATIC_ROOT names no directory to collect into.")

    destination_root = Path(root).resolve()
    sources = _sources()
    if not sources:
        raise click.ClickException("STATIC_DIRS names no directory to collect from.")

    if clear and destination_root.exists():
        shutil.rmtree(destination_root)
    destination_root.mkdir(parents=True, exist_ok=True)

    copied = unchanged = 0
    taken: set[Path] = set()

    for source in sources:
        if not source.is_dir():
            click.echo(f"  {click.style('!', fg='yellow')} {source} is not a directory; skipped")
            continue

        for path in _files(source):
            relative = path.relative_to(source)
            if relative in taken:
                continue
            taken.add(relative)

            destination = destination_root / relative
            if _current(path, destination):
                unchanged += 1
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            copied += 1

    click.echo(f"{copied} copied, {unchanged} unchanged, into {destination_root}")


def _current(source: Path, destination: Path) -> bool:
    """Whether `destination` already holds what `source` has.

    Size and modification time rather than the contents: reading every file to
    compare it would make collecting cost as much as copying, which is what the
    comparison is there to avoid.
    """
    if not destination.exists():
        return False
    original, copy = source.stat(), destination.stat()
    return original.st_size == copy.st_size and int(original.st_mtime) == int(copy.st_mtime)
