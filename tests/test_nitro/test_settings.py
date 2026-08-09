import sys
import textwrap

import pytest

from nitro.settings import (
    ENVIRONMENT_VARIABLE,
    ImproperlyConfigured,
    LazySettings,
    ServerOptions,
    Settings,
)


def write_settings_module(tmp_path, monkeypatch, name, body):
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(body))
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(name, None)
    return name


class TestSettings:
    def test_defaults_are_loaded(self):
        assert Settings().DEBUG is False
        assert Settings().SERVER["PORT"] == 8000

    def test_project_module_overrides_defaults(self, tmp_path, monkeypatch):
        name = write_settings_module(
            tmp_path,
            monkeypatch,
            "project_settings",
            """
            DEBUG = True
            SECRET_KEY = "s3cret"
            """,
        )
        resolved = Settings(name)
        assert resolved.DEBUG is True
        assert resolved.SECRET_KEY == "s3cret"
        # Untouched defaults survive.
        assert resolved.TIME_ZONE == "Europe/Zurich"

    def test_lowercase_names_are_ignored(self, tmp_path, monkeypatch):
        name = write_settings_module(
            tmp_path, monkeypatch, "lowercase_settings", "debug = True\n"
        )
        assert Settings(name).DEBUG is False

    def test_a_missing_module_is_reported_clearly(self):
        with pytest.raises(ImproperlyConfigured, match="no_such_settings_module"):
            Settings("no_such_settings_module")


class TestLazySettings:
    def test_resolution_is_deferred(self, monkeypatch):
        monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)
        lazy = LazySettings()
        assert lazy.configured is False
        assert repr(lazy) == "<LazySettings [unresolved]>"

        assert lazy.DEBUG is False
        assert lazy.configured is True
        assert repr(lazy) == "<LazySettings [defaults]>"

    def test_the_environment_variable_selects_the_module(self, tmp_path, monkeypatch):
        name = write_settings_module(
            tmp_path, monkeypatch, "env_settings", "DEBUG = True\n"
        )
        monkeypatch.setenv(ENVIRONMENT_VARIABLE, name)

        lazy = LazySettings()
        assert lazy.DEBUG is True
        assert name in repr(lazy)

    def test_private_names_do_not_trigger_resolution(self, monkeypatch):
        monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)
        lazy = LazySettings()
        with pytest.raises(AttributeError):
            lazy._not_a_setting
        assert lazy.configured is False

    def test_assignment_writes_through(self, monkeypatch):
        monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)
        lazy = LazySettings()
        lazy.DEBUG = True
        assert lazy.DEBUG is True

    def test_reset_reloads_on_next_read(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)
        lazy = LazySettings()
        assert lazy.DEBUG is False

        name = write_settings_module(
            tmp_path, monkeypatch, "reset_settings", "DEBUG = True\n"
        )
        monkeypatch.setenv(ENVIRONMENT_VARIABLE, name)
        lazy.reset()
        assert lazy.DEBUG is True


class FakeSettings:
    def __init__(self, server):
        self.SERVER = server


class TestServerOptions:
    def test_defaults(self):
        options = ServerOptions.resolve(FakeSettings({}))
        assert (options.host, options.port, options.workers) == ("localhost", 8000, 1)

    def test_settings_are_read_case_insensitively_from_upper_case_keys(self):
        options = ServerOptions.resolve(FakeSettings({"HOST": "0.0.0.0", "PORT": 9000}))
        assert options.host == "0.0.0.0"
        assert options.port == 9000

    def test_overrides_beat_settings(self):
        options = ServerOptions.resolve(FakeSettings({"PORT": 9000}), port=9500)
        assert options.port == 9500

    def test_an_absent_override_does_not_erase_a_setting(self):
        options = ServerOptions.resolve(FakeSettings({"PORT": 9000}), port=None)
        assert options.port == 9000

    def test_an_unknown_setting_is_rejected(self):
        with pytest.raises(ImproperlyConfigured, match="NONSENSE"):
            ServerOptions.resolve(FakeSettings({"NONSENSE": 1}))

    def test_an_unknown_override_is_rejected(self):
        with pytest.raises(ImproperlyConfigured, match="nonsense"):
            ServerOptions.resolve(FakeSettings({}), nonsense=1)
