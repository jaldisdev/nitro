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

"""Restart the server when a source file changes.

Reloading is a supervisor, not a mode the server runs in. A parent process
watches the tree and owns nothing else; the server runs in a child that is
replaced wholesale on a change. That split is what keeps the cost off the
serving path: the child is started by the same command line the user typed,
reaches the same :func:`nitro.app.build_server` call it would have reached
anyway, and never imports this module. A server started without ``--reload``
does not import it either.

Replacing the process rather than re-importing the changed module is the only
approach that is actually correct here. The route table is compiled into the
matcher at startup, the middleware stack is built once from settings, and the
listening sockets belong to the Rust server; swapping a function object in
``sys.modules`` would leave all three pointing at the old code.

The watcher polls. A change is noticed within :data:`POLL_INTERVAL` rather
than immediately, which is the trade for having no dependency to install and
no notification backend to differ per platform — and this process only ever
runs on a developer's machine.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

#: Set on the child so it runs the server instead of becoming a supervisor
#: itself. Without it, a script whose ``__main__`` calls ``serve(reload=True)``
#: would fork supervisors forever, since the child re-runs that same script.
CHILD_ENVIRONMENT_MARKER = "NITRO_RELOAD_CHILD"

#: Never descended into. Package installs and build outputs hold far more files
#: than a project does, and none of them change while the developer is editing.
IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".bzr",
        ".git",
        ".hg",
        ".svn",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "__pycache__",
        "node_modules",
        "target",
        "venv",
        ".venv",
    }
)

WATCHED_SUFFIXES = frozenset({".py"})

POLL_INTERVAL = 0.4

#: Waited out after the first change before the snapshot is retaken, so that
#: saving several files at once — a formatter writing a whole package — is one
#: restart rather than one per file.
SETTLE_INTERVAL = 0.2

#: How long a child gets to finish draining after SIGTERM before it is killed.
#: The server drains gracefully on that signal, and a developer's reload should
#: not wait out a production drain timeout for it.
TERMINATE_GRACE = 5.0


def is_reload_child() -> bool:
    """Whether this process is the server child of a reload supervisor."""
    return os.environ.get(CHILD_ENVIRONMENT_MARKER) == "1"


def child_command() -> list[str]:
    """The command that re-runs whatever the user typed.

    Both entry points have to survive this. ``python -m nitro`` is recovered
    from ``__main__``'s spec, because its ``sys.argv[0]`` is the package's
    ``__main__.py`` and re-running that file directly would leave the package
    unimported. Everything else — a console script, an executable project file
    — is a path Python can run, so it is passed back with the interpreter in
    front of it rather than relying on its shebang.
    """
    main_module = sys.modules["__main__"]
    spec = getattr(main_module, "__spec__", None)
    if spec is not None and spec.parent:
        return [sys.executable, "-m", spec.parent, *sys.argv[1:]]
    return [sys.executable, *sys.argv]


def iter_watched_files(roots: Sequence[Path]) -> Iterator[Path]:
    """Every file under `roots` whose suffix is watched.

    Pruned during the walk rather than filtered afterwards: descending into a
    virtual environment to discard its contents costs the same as watching it.
    """
    for root in roots:
        for directory, subdirectories, filenames in os.walk(root):
            subdirectories[:] = [
                name for name in subdirectories if name not in IGNORED_DIRECTORY_NAMES
            ]
            for filename in filenames:
                if Path(filename).suffix in WATCHED_SUFFIXES:
                    yield Path(directory) / filename


def snapshot(roots: Sequence[Path]) -> dict[Path, float]:
    """Modification times for every watched file, keyed by path."""
    times: dict[Path, float] = {}
    for path in iter_watched_files(roots):
        try:
            times[path] = path.stat().st_mtime
        except OSError:
            # Editors write through temporary files, so a path listed a moment
            # ago may already be gone. Its absence is itself a change, and the
            # next snapshot reports it as one.
            continue
    return times


def first_difference(before: dict[Path, float], after: dict[Path, float]) -> Path | None:
    """A path that differs between two snapshots, or `None` if none does."""
    for path, modified in after.items():
        if before.get(path) != modified:
            return path
    for path in before:
        if path not in after:
            return path
    return None


def _spawn(command: Sequence[str]) -> subprocess.Popen[bytes]:
    environment = {**os.environ, CHILD_ENVIRONMENT_MARKER: "1"}
    return subprocess.Popen(command, env=environment)


def _stop(process: subprocess.Popen[bytes]) -> None:
    """End `process`, without waiting out a full production drain for it."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_with_reloader(roots: Sequence[Path] | None = None) -> int:
    """Supervise the server, restarting it when a watched file changes.

    Returns the exit status the caller should exit with. Blocks until the
    child exits on its own or the supervisor is interrupted.

    A child that crashes is not restarted on a timer — the supervisor waits for
    an edit first. A syntax error would otherwise become a restart loop that
    buries the traceback that explains it.
    """
    watched = [Path(root) for root in roots] if roots is not None else [Path.cwd()]
    command = child_command()

    for root in watched:
        print(f"Watching {root} for changes", flush=True)

    # SIGTERM has to reach the child too. Raising KeyboardInterrupt from the
    # handler routes it into the same shutdown path an interactive Ctrl+C
    # takes, rather than duplicating it.
    def _interrupt(_signal_number: int, _frame: object) -> None:
        raise KeyboardInterrupt

    previous_term = signal.signal(signal.SIGTERM, _interrupt)
    try:
        while True:
            before = snapshot(watched)
            process = _spawn(command)
            try:
                changed = _supervise(process, watched, before)
            except KeyboardInterrupt:
                _stop(process)
                return 0

            if changed is not None:
                _stop(process)
                print(f"{changed} changed, restarting", flush=True)
                continue

            status = process.returncode
            if status == 0:
                return 0
            print(f"Server exited with status {status}; waiting for a change", flush=True)
            try:
                changed = _wait_for_change(watched, snapshot(watched))
            except KeyboardInterrupt:
                return 0
            print(f"{changed} changed, restarting", flush=True)
    finally:
        signal.signal(signal.SIGTERM, previous_term)


def _supervise(
    process: subprocess.Popen[bytes],
    roots: Sequence[Path],
    before: dict[Path, float],
) -> Path | None:
    """Watch until a file changes or `process` exits.

    Returns the changed path, or `None` if the child exited first.
    """
    while True:
        if process.poll() is not None:
            return None
        time.sleep(POLL_INTERVAL)
        changed = first_difference(before, snapshot(roots))
        if changed is not None:
            time.sleep(SETTLE_INTERVAL)
            return changed


def _wait_for_change(roots: Sequence[Path], before: dict[Path, float]) -> Path:
    """Block until a watched file changes, and say which one did."""
    while True:
        time.sleep(POLL_INTERVAL)
        changed = first_difference(before, snapshot(roots))
        if changed is not None:
            time.sleep(SETTLE_INTERVAL)
            return changed
