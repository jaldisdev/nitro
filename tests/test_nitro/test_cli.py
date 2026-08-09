import click
import pytest
from click.testing import CliRunner

from nitro import __version__
from nitro.cli import cli, commands_in, load_project_commands, register_commands
from nitro.settings import settings


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def full_cli():
    """The command group with the built-in commands registered."""
    group = click.Group(name="nitro")
    register_commands(group, "nitro.cli.commands")
    return group


class TestGroup:
    def test_the_version_flag_reports_the_version(self, runner):
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_help_is_available_under_both_spellings(self, runner):
        for flag in ("-h", "--help"):
            result = runner.invoke(cli, [flag])
            assert result.exit_code == 0
            assert "Manage and run Nitro projects" in result.output


class TestDiscovery:
    def test_the_built_in_commands_are_found(self):
        found = {command.name for command in commands_in("nitro.cli.commands")}
        assert {"run", "version", "shell", "check"} <= found

    def test_an_unknown_package_is_reported(self):
        with pytest.raises(ImportError, match="no.such.package"):
            commands_in("no.such.package")

    def test_project_commands_are_registered(self, tmp_path, monkeypatch):
        package = tmp_path / "project_commands"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "greet.py").write_text(
            "import click\n\n\n@click.command('greet')\ndef greet():\n    click.echo('hello')\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setattr(settings, "COMMAND_MODULES", ["project_commands"], raising=False)

        group = click.Group(name="nitro")
        load_project_commands(group)

        assert "greet" in group.commands

    def test_a_broken_package_does_not_stop_the_others(self, monkeypatch, caplog):
        monkeypatch.setattr(
            settings, "COMMAND_MODULES", ["no.such.package"], raising=False
        )
        group = click.Group(name="nitro")

        load_project_commands(group)

        assert group.commands == {}
        assert "no.such.package" in caplog.text

    def test_unreadable_settings_leave_the_built_ins_working(self, monkeypatch, caplog):
        class Exploding:
            @property
            def COMMAND_MODULES(self):
                raise RuntimeError("settings are broken")

        monkeypatch.setattr("nitro.settings.settings", Exploding())
        group = click.Group(name="nitro")

        load_project_commands(group)

        assert "COMMAND_MODULES" in caplog.text


class TestVersion:
    def test_it_reports_both_versions(self, runner, full_cli):
        result = runner.invoke(full_cli, ["version"])
        assert result.exit_code == 0
        assert __version__ in result.output
        assert "Python" in result.output


class TestCheck:
    def test_a_configured_project_passes(self, runner, full_cli, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", True, raising=False)
        monkeypatch.setattr(settings, "SERVER", {"HTTP": "1"}, raising=False)

        result = runner.invoke(full_cli, ["check"])

        assert result.exit_code == 0, result.output
        assert "No problems found" in result.output

    def test_http3_without_a_certificate_is_reported(self, runner, full_cli, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", True, raising=False)
        monkeypatch.setattr(settings, "SERVER", {"HTTP": "3"}, raising=False)

        result = runner.invoke(full_cli, ["check"])

        assert result.exit_code != 0
        assert "TLS certificate" in result.output

    def test_a_production_project_without_a_secret_is_reported(
        self, runner, full_cli, monkeypatch
    ):
        monkeypatch.setattr(settings, "DEBUG", False, raising=False)
        monkeypatch.setattr(settings, "SECRET_KEY", "", raising=False)
        monkeypatch.setattr(settings, "ALLOWED_HOSTS", ["example.test"], raising=False)
        monkeypatch.setattr(settings, "SERVER", {"HTTP": "1"}, raising=False)

        result = runner.invoke(full_cli, ["check"])

        assert result.exit_code != 0
        assert "SECRET_KEY" in result.output

    def test_optional_packages_are_listed_when_asked(self, runner, full_cli, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG", True, raising=False)
        monkeypatch.setattr(settings, "SERVER", {"HTTP": "1"}, raising=False)

        result = runner.invoke(full_cli, ["check", "-v"])

        assert "Optional packages" in result.output
        assert "redis" in result.output


class TestRun:
    def test_a_malformed_specifier_is_rejected(self, runner, full_cli):
        result = runner.invoke(full_cli, ["run", "not-a-specifier"])
        assert result.exit_code != 0
        assert "module:attribute" in result.output

    def test_an_unimportable_module_is_reported(self, runner, full_cli):
        result = runner.invoke(full_cli, ["run", "no_such_module:app"])
        assert result.exit_code != 0
        assert "no_such_module" in result.output

    def test_a_missing_attribute_is_reported(self, runner, full_cli, tmp_path, monkeypatch):
        (tmp_path / "empty_app.py").write_text("")
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(full_cli, ["run", "empty_app:app"])

        assert result.exit_code != 0
        assert "app" in result.output


class TestShell:
    def test_a_missing_shell_is_reported(self, runner, full_cli, monkeypatch):
        monkeypatch.setitem(__import__("sys").modules, "IPython", None)
        result = runner.invoke(full_cli, ["shell", "-i", "ipython"])
        assert result.exit_code != 0

    def test_the_namespace_carries_the_project(self):
        from nitro.cli.commands.shell import build_namespace

        namespace = build_namespace()
        assert "nitro" in namespace
        assert "settings" in namespace
