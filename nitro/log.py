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

"""Python logging, configured from the ``LOGGING`` setting.

This is the log of the framework and the application. The compiled server keeps
a log of its own, configured by the ``SERVER_LOG_*`` settings, and the two are
deliberately independent.

Nothing here runs on import. Logging is configured by the entry points that
start a process — the command line and :meth:`nitro.Nitro.serve` — so an
application embedded in something else, or built by a test, leaves the host's
logging alone.
"""

from __future__ import annotations

import copy
import logging
import logging.config
from datetime import UTC, datetime
from typing import Any

from nitro.settings import ImproperlyConfigured, settings

logger = logging.getLogger("nitro")

#: The sections of a ``dictConfig`` mapping whose entries are merged by name.
MERGED_SECTIONS: tuple[str, ...] = ("formatters", "filters", "handlers", "loggers")

#: The server's log spells these the way `tracing` does, and the two logs share
#: stderr by default.
SHORT_LEVEL_NAMES: dict[str, str] = {"WARNING": "WARN"}

DEFAULT_LOGGING: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "nitro": {"()": "nitro.log.NitroFormatter"},
    },
    "handlers": {
        "nitro": {"class": "logging.StreamHandler", "formatter": "nitro"},
    },
    "loggers": {
        "nitro": {"handlers": ["nitro"], "level": "INFO"},
    },
}


class NitroFormatter(logging.Formatter):
    """Lays a record out the way the server's text log does.

    ``2026-09-17T10:12:03.123456Z  WARN nitro.mail: message`` — so lines from
    both logs read alike, and sort together, when they share a stream.
    """

    def __init__(self) -> None:
        # The format string is what makes `Formatter.format` fill in `asctime`;
        # the layout itself is `formatMessage`'s.
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, UTC)
        return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def formatMessage(self, record: logging.LogRecord) -> str:
        level = SHORT_LEVEL_NAMES.get(record.levelname, record.levelname)
        return f"{record.asctime} {level:>5} {record.name}: {record.message}"


def merged_logging(overrides: dict[str, Any]) -> dict[str, Any]:
    """`overrides` laid over :data:`DEFAULT_LOGGING`, entry by entry.

    Copied two levels deep because `dictConfig` pops keys out of each entry it
    reads, and the setting has to survive being applied more than once. No
    deeper, since an entry may hold an object — a stream, say — that cannot be
    copied.
    """
    merged = copy.deepcopy(DEFAULT_LOGGING)
    for key, value in overrides.items():
        if key in MERGED_SECTIONS:
            section = {name: dict(entry) for name, entry in value.items()}
            merged.setdefault(key, {}).update(section)
        else:
            merged[key] = value
    return merged


def apply_logging_settings(source: Any = None) -> str | None:
    """Configure logging from ``LOGGING``, and say what was wrong with it.

    A ``LOGGING`` that cannot be applied is replaced by the defaults rather
    than stopping the process, and the problem comes back as a description.
    Settings that do not load at all get the defaults too, and ``None``: that
    failure is reported wherever the settings are next read.
    """
    settings_source = settings if source is None else source
    try:
        overrides = getattr(settings_source, "LOGGING", {})
    except ImproperlyConfigured:
        overrides = {}

    if not isinstance(overrides, dict):
        logging.config.dictConfig(copy.deepcopy(DEFAULT_LOGGING))
        return f"LOGGING must be a dict, got {type(overrides).__name__}"

    try:
        merged = merged_logging(overrides)
        logging.config.dictConfig(merged)
    except (ValueError, TypeError, AttributeError, ImportError) as error:
        # A configuration that failed partway leaves some of it applied; the
        # defaults put every logger they name back into a known state.
        logging.config.dictConfig(copy.deepcopy(DEFAULT_LOGGING))
        cause = error.__cause__
        return f"{error}: {cause}" if cause is not None else str(error)
    return None


def configure_logging(source: Any = None) -> None:
    """Configure logging from ``LOGGING``, warning when it cannot be applied."""
    problem = apply_logging_settings(source)
    if problem is not None:
        logger.warning("ignoring the LOGGING setting: %s", problem)
