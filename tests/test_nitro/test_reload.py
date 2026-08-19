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

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from nitro.reload import (
    CHILD_ENVIRONMENT_MARKER,
    IGNORED_DIRECTORY_NAMES,
    child_command,
    first_difference,
    is_reload_child,
    iter_watched_files,
    snapshot,
)


@pytest.fixture
def tree(tmp_path):
    """A project tree with a file of every kind the walk has to judge."""
    (tmp_path / "app.py").write_text("value = 1\n")
    (tmp_path / "notes.txt").write_text("not watched\n")
    package = tmp_path / "package"
    package.mkdir()
    (package / "views.py").write_text("value = 2\n")
    for ignored in IGNORED_DIRECTORY_NAMES:
        directory = tmp_path / ignored
        directory.mkdir()
        (directory / "buried.py").write_text("value = 3\n")
    return tmp_path


class TestWatchedFiles:
    def test_only_python_files_are_watched(self, tree):
        found = {path.name for path in iter_watched_files([tree])}
        assert found == {"app.py", "views.py"}

    def test_ignored_directories_are_never_descended_into(self, tree):
        # The point is the pruning, not the filtering: a virtual environment
        # holds more files than the project does, and walking it to throw the
        # results away would cost the same as watching it.
        for path in iter_watched_files([tree]):
            assert not set(path.parts) & IGNORED_DIRECTORY_NAMES

    def test_a_missing_file_does_not_stop_the_snapshot(self, tree, monkeypatch):
        real_stat = Path.stat

        def vanishing_stat(self, *args, **kwargs):
            if self.name == "app.py":
                raise FileNotFoundError(self)
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", vanishing_stat)
        taken = snapshot([tree])
        assert not any(path.name == "app.py" for path in taken)
        assert any(path.name == "views.py" for path in taken)


class TestChangeDetection:
    def test_an_unchanged_tree_reports_nothing(self, tree):
        before = snapshot([tree])
        assert first_difference(before, snapshot([tree])) is None

    def test_a_modified_file_is_reported(self, tree):
        before = snapshot([tree])
        target = tree / "package" / "views.py"
        target.write_text("value = 99\n")
        # Coarse filesystem timestamps would hide an edit made this quickly.
        target.touch()
        assert first_difference(before, snapshot([tree])) == target

    def test_a_new_file_is_reported(self, tree):
        before = snapshot([tree])
        (tree / "added.py").write_text("value = 4\n")
        assert first_difference(before, snapshot([tree])) == tree / "added.py"

    def test_a_deleted_file_is_reported(self, tree):
        before = snapshot([tree])
        (tree / "app.py").unlink()
        assert first_difference(before, snapshot([tree])) == tree / "app.py"


class TestChildIdentification:
    def test_the_marker_identifies_the_child(self, monkeypatch):
        monkeypatch.delenv(CHILD_ENVIRONMENT_MARKER, raising=False)
        assert not is_reload_child()
        monkeypatch.setenv(CHILD_ENVIRONMENT_MARKER, "1")
        assert is_reload_child()

    def test_a_script_invocation_is_rerun_through_the_interpreter(self, monkeypatch):
        # A project file is executable and has a shebang, but the child is
        # started with an explicit interpreter so it cannot depend on that.
        monkeypatch.setattr(sys, "argv", ["./dummy/main.py"])
        monkeypatch.setattr(sys.modules["__main__"], "__spec__", None, raising=False)
        assert child_command() == [sys.executable, "./dummy/main.py"]

    def test_a_module_invocation_is_rerun_as_a_module(self, monkeypatch):
        # `python -m nitro.cli` leaves `__main__.py` in argv[0]; re-running
        # that path directly would leave the package unimported.
        class Spec:
            parent = "nitro.cli"

        monkeypatch.setattr(sys, "argv", ["/site-packages/nitro/cli/__main__.py", "app:app"])
        monkeypatch.setattr(sys.modules["__main__"], "__spec__", Spec(), raising=False)
        assert child_command() == [sys.executable, "-m", "nitro.cli", "app:app"]


class TestCommandLine:
    def test_the_flag_is_offered_and_off_by_default(self):
        from click.testing import CliRunner

        from nitro.cli.serve import serve

        result = CliRunner().invoke(serve, ["--help"])
        assert result.exit_code == 0
        assert "--reload" in result.output

        option = next(o for o in serve.params if o.name == "reload")
        assert option.default is False, "reloading must be opt-in"

    def test_reloading_supervises_instead_of_loading_the_application(self, monkeypatch):
        # The parent must not import the project: it owns the watch loop and
        # nothing else, and importing would hold a copy of the old code.
        from click.testing import CliRunner

        import nitro.reload as reload_module

        # `nitro.cli` does `from nitro.cli.serve import serve`, which rebinds
        # the `serve` attribute on the package from the module to the command.
        # `import nitro.cli.serve as ...` therefore hands back the command.
        serve_module = sys.modules["nitro.cli.serve"]

        def refuse(specifier):
            raise AssertionError("the supervisor imported the application")

        monkeypatch.setattr(serve_module, "load_application", refuse)
        monkeypatch.setattr(reload_module, "run_with_reloader", lambda *args, **kwargs: 7)
        monkeypatch.delenv(CHILD_ENVIRONMENT_MARKER, raising=False)

        result = CliRunner().invoke(serve_module.serve, ["--reload", "app:app"])
        assert result.exit_code == 7


class TestServingCostsNothing:
    """The reloader must be absent from the serving path entirely."""

    def test_building_a_server_does_not_import_the_reloader(self, tmp_path):
        # A subprocess, because any other test in this session may already
        # have imported the module this one is asserting the absence of.
        script = textwrap.dedent(
            """
            import sys
            from nitro.app import Nitro, build_server

            build_server(Nitro(http="1", log_level="warning"), port=0)
            assert "nitro.reload" not in sys.modules, "the serving path imported the reloader"
            print("clean")
            """
        )
        source = tmp_path / "check.py"
        source.write_text(script)
        completed = subprocess.run(
            [sys.executable, str(source)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "clean" in completed.stdout

    def test_serving_without_reload_never_consults_the_marker(self, monkeypatch):
        # Set the marker to what a child carries. Serving normally must not
        # read it, so the flag alone cannot change how a server starts.
        monkeypatch.setenv(CHILD_ENVIRONMENT_MARKER, "1")
        from nitro.app import Nitro, build_server

        server, _options = build_server(Nitro(http="1", log_level="warning"), port=0)
        assert server is not None
