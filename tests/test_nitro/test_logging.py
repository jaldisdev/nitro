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

import io
import logging
import os
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

from nitro.log import (
    DEFAULT_LOGGING,
    NitroFormatter,
    apply_logging_settings,
    configure_logging,
)
from nitro.settings import ImproperlyConfigured

TIMESTAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z"

BROKEN_LOGGING = {
    "handlers": {"broken": {"class": "nowhere.MissingHandler"}},
    "loggers": {"myproject": {"handlers": ["broken"]}},
}


class UnloadableSettings:
    @property
    def LOGGING(self):
        raise ImproperlyConfigured("the settings module does not import")


def _record(name: str, level: int, message: str) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, message, None, None)


class TestFormatter:
    def test_a_record_is_laid_out_like_the_server_log(self):
        line = NitroFormatter().format(_record("nitro.mail", logging.INFO, "sent"))

        assert re.fullmatch(rf"{TIMESTAMP}  INFO nitro\.mail: sent", line), line

    def test_warnings_use_the_servers_spelling(self):
        line = NitroFormatter().format(_record("nitro", logging.WARNING, "careful"))

        assert re.fullmatch(rf"{TIMESTAMP}  WARN nitro: careful", line), line

    def test_a_traceback_follows_the_line(self):
        try:
            raise RuntimeError("deliberate")
        except RuntimeError:
            record = logging.LogRecord(
                "nitro", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
            )

        first, *rest = NitroFormatter().format(record).splitlines()

        assert first.endswith("ERROR nitro: failed")
        assert rest[-1] == "RuntimeError: deliberate"


@pytest.mark.usefixtures("isolated_logging")
class TestDefaults:
    def test_framework_records_reach_stderr(self, capsys):
        assert apply_logging_settings(SimpleNamespace(LOGGING={})) is None

        logging.getLogger("nitro.endpoints").info("visible")
        logging.getLogger("nitro.endpoints").debug("hidden")

        error = capsys.readouterr().err
        assert re.search(rf"^{TIMESTAMP}  INFO nitro\.endpoints: visible$", error, re.M), error
        assert "hidden" not in error

    def test_the_root_logger_is_left_alone(self):
        handlers = logging.root.handlers[:]
        level = logging.root.level

        apply_logging_settings(SimpleNamespace(LOGGING={}))

        assert logging.root.handlers == handlers
        assert logging.root.level == level

    def test_unloadable_settings_get_the_defaults(self, capsys):
        assert apply_logging_settings(UnloadableSettings()) is None

        logging.getLogger("nitro").warning("still logged")

        assert "WARN nitro: still logged" in capsys.readouterr().err


@pytest.mark.usefixtures("isolated_logging")
class TestMerging:
    def test_a_level_can_be_changed_without_restating_the_handler(self, capsys):
        configuration = {"loggers": {"nitro": {"handlers": ["nitro"], "level": "DEBUG"}}}

        assert apply_logging_settings(SimpleNamespace(LOGGING=configuration)) is None
        logging.getLogger("nitro.di").debug("now visible")

        assert "DEBUG nitro.di: now visible" in capsys.readouterr().err

    def test_a_default_handler_can_be_replaced_by_name(self, capsys):
        stream = io.StringIO()
        configuration = {
            "handlers": {"nitro": {"class": "logging.StreamHandler", "stream": stream}}
        }

        assert apply_logging_settings(SimpleNamespace(LOGGING=configuration)) is None
        logging.getLogger("nitro").warning("redirected")

        assert stream.getvalue() == "redirected\n"
        assert "redirected" not in capsys.readouterr().err

    def test_existing_loggers_stay_enabled(self):
        existing = logging.getLogger("myproject.views")
        configuration = {"loggers": {"myproject.models": {"level": "INFO"}}}

        assert apply_logging_settings(SimpleNamespace(LOGGING=configuration)) is None

        assert not existing.disabled
        assert not logging.getLogger("nitro.app").disabled

    def test_the_setting_survives_being_applied_twice(self):
        configuration = {
            "formatters": {"plain": {"()": "logging.Formatter", "fmt": "%(message)s"}},
            "handlers": {"nitro": {"class": "logging.StreamHandler", "formatter": "plain"}},
        }
        source = SimpleNamespace(LOGGING=configuration)

        assert apply_logging_settings(source) is None
        assert apply_logging_settings(source) is None
        assert "()" in configuration["formatters"]["plain"]

    def test_the_defaults_are_not_changed_by_applying_them(self):
        before = repr(DEFAULT_LOGGING)

        apply_logging_settings(SimpleNamespace(LOGGING={"loggers": {"nitro": {"level": "ERROR"}}}))

        assert repr(DEFAULT_LOGGING) == before


@pytest.mark.usefixtures("isolated_logging")
class TestInvalidSetting:
    def test_a_handler_that_cannot_be_built_is_described(self):
        problem = apply_logging_settings(SimpleNamespace(LOGGING=BROKEN_LOGGING))

        assert problem is not None
        assert "Unable to configure handler 'broken'" in problem
        assert "nowhere" in problem

    def test_a_setting_that_is_not_a_mapping_is_described(self):
        problem = apply_logging_settings(SimpleNamespace(LOGGING=["console"]))

        assert problem == "LOGGING must be a dict, got list"

    def test_a_malformed_section_is_described(self):
        problem = apply_logging_settings(SimpleNamespace(LOGGING={"loggers": ["nitro"]}))

        assert problem is not None

    def test_the_defaults_still_work_afterwards(self, capsys):
        apply_logging_settings(SimpleNamespace(LOGGING=BROKEN_LOGGING))

        logging.getLogger("nitro").error("after the failure")

        assert "ERROR nitro: after the failure" in capsys.readouterr().err

    def test_configuring_warns_in_the_nitro_layout(self, capsys):
        configure_logging(SimpleNamespace(LOGGING=BROKEN_LOGGING))

        error = capsys.readouterr().err
        assert re.search(
            rf"^{TIMESTAMP}  WARN nitro: ignoring the LOGGING setting: Unable to configure",
            error,
            re.M,
        ), error


def _write_settings(directory, logging_setting: str) -> None:
    (directory / "project_settings.py").write_text(f"DEBUG = True\nLOGGING = {logging_setting}\n")


class TestEntryPoints:
    def test_building_an_application_leaves_logging_alone(self, tmp_path):
        probe = (
            "import logging\n"
            "from nitro import Nitro\n"
            "Nitro()\n"
            "print(logging.getLogger('nitro').handlers)\n"
        )

        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.strip() == "[]"

    def test_the_command_line_carries_on_with_an_invalid_setting(self, tmp_path):
        _write_settings(tmp_path, repr(BROKEN_LOGGING))

        result = subprocess.run(
            [sys.executable, "-m", "nitro.cli", "version"],
            cwd=tmp_path,
            env=dict(os.environ, NITRO_SETTINGS_MODULE="project_settings"),
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert "WARN nitro: ignoring the LOGGING setting" in result.stderr

    @pytest.mark.parametrize("script", [False, True], ids=["command-line", "app-serve"])
    def test_serving_carries_on_with_an_invalid_setting(
        self, server_factory, hello_app, tmp_path, monkeypatch, script
    ):
        _write_settings(tmp_path, repr(BROKEN_LOGGING))
        monkeypatch.setenv("NITRO_SETTINGS_MODULE", "project_settings")
        source = hello_app + "\n        if __name__ == '__main__':\n            app.serve(port=0)\n"

        server = server_factory(source, script=script)

        assert server.request("/").text == "hello"
        assert server.stop() == 0
        assert "WARN nitro: ignoring the LOGGING setting" in server.output

    def test_a_handler_failure_is_logged_through_the_setting(
        self, server_factory, tmp_path, monkeypatch
    ):
        log_file = tmp_path / "application.log"
        configuration = {
            "handlers": {"nitro": {"class": "logging.FileHandler", "filename": str(log_file)}},
        }
        _write_settings(tmp_path, repr(configuration))
        monkeypatch.setenv("NITRO_SETTINGS_MODULE", "project_settings")

        server = server_factory(
            """
            from nitro import Nitro

            app = Nitro(http="1", log_level="warning")

            @app.route("/boom")
            async def boom(request):
                raise RuntimeError("deliberate")
            """
        )

        assert server.request("/boom").status == 500
        assert server.stop() == 0
        assert "RuntimeError: deliberate" in log_file.read_text()
