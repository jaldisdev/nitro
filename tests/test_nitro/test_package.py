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
