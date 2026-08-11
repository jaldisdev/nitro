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

import importlib

import nitro


def test_version_is_exposed() -> None:
    assert isinstance(nitro.__version__, str)
    assert nitro.__version__.count(".") >= 2


def test_compiled_extension_is_importable() -> None:
    extension = importlib.import_module("nitro._nitro")
    assert extension.__doc__


def test_cli_group_reports_version() -> None:
    from click.testing import CliRunner

    from nitro.cli import cli

    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert nitro.__version__ in result.output


def test_every_module_parses() -> None:
    """A module nothing imports still has to be valid Python.

    Two have been shipped with a stray quote in them, and neither surfaced:
    a syntax error in a module no test imports goes unnoticed until whoever
    first reaches for it.
    """
    import ast
    import pathlib

    root = pathlib.Path(nitro.__file__).parent
    broken = []
    for path in sorted(root.rglob("*.py")):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            broken.append(f"{path.relative_to(root)}:{error.lineno}: {error.msg}")

    assert not broken, "modules that cannot be imported: " + ", ".join(broken)
