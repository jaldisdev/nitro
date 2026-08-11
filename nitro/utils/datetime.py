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

"""Time zones and aware/naive datetimes, for application code.

The current time zone is held in a context variable rather than a thread local:
a Nitro application serves many connections on one thread, so a thread local
would let one request's time zone be read by another.
"""

from __future__ import annotations

import zoneinfo
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, tzinfo

from nitro.settings import settings

__all__ = [
    "activate",
    "deactivate",
    "get_current_timezone",
    "get_current_timezone_name",
    "get_default_timezone",
    "is_aware",
    "is_naive",
    "localtime",
    "make_aware",
    "make_naive",
    "now",
    "override",
]

#: The time zone in force for the current task, when one has been activated.
#: A context variable rather than a thread local, because the thread is shared
#: by every connection this worker is serving.
_active: ContextVar[tzinfo | None] = ContextVar("nitro_timezone", default=None)


# ── time zones ───────────────────────────────────────────────────────────────


def get_default_timezone() -> tzinfo:
    """The time zone named by ``TIME_ZONE``."""
    return zoneinfo.ZoneInfo(settings.TIME_ZONE)


def get_current_timezone() -> tzinfo:
    """The time zone in force here, or the default when none was activated."""
    active = _active.get()
    return active if active is not None else get_default_timezone()


def get_current_timezone_name() -> str:
    """The name of the time zone in force here."""
    return _timezone_name(get_current_timezone())


def activate(timezone: tzinfo | str) -> None:
    """Use `timezone` from here on, for this task."""
    _active.set(zoneinfo.ZoneInfo(timezone) if isinstance(timezone, str) else timezone)


def deactivate() -> None:
    """Go back to the default time zone."""
    _active.set(None)


@contextmanager
def override(timezone: tzinfo | str | None) -> Iterator[None]:
    """Use `timezone` for the body, then restore what was in force.

        with override("Europe/Zurich"):
            ...

    `None` deactivates for the body, so the default applies.
    """
    token = _active.set(
        None
        if timezone is None
        else zoneinfo.ZoneInfo(timezone)
        if isinstance(timezone, str)
        else timezone
    )
    try:
        yield
    finally:
        _active.reset(token)


def _timezone_name(timezone: tzinfo) -> str:
    """The offset for a fixed-offset zone, or the zone's name."""
    return timezone.tzname(None) or str(timezone)


# ── local time ───────────────────────────────────────────────────────────────


def now() -> datetime:
    """The current moment, aware or naive as ``USE_TZ`` says."""
    return datetime.now(tz=UTC if settings.USE_TZ else None)


def is_aware(value: datetime) -> bool:
    """Whether `value` carries a time zone.

    Defined in Python's own terms: an aware datetime is one whose `utcoffset`
    answers something.
    """
    return value.utcoffset() is not None


def is_naive(value: datetime) -> bool:
    """Whether `value` carries no time zone."""
    return value.utcoffset() is None


def make_aware(value: datetime, timezone: tzinfo | None = None) -> datetime:
    """Attach a time zone to a naive datetime.

    Uses `fold` to resolve a time that occurs twice, which is what the standard
    library does for an ambiguous local time around a DST change.
    """
    if timezone is None:
        timezone = get_current_timezone()
    if is_aware(value):
        raise ValueError(f"make_aware expects a naive datetime, got {value}")
    return value.replace(tzinfo=timezone)


def make_naive(value: datetime, timezone: tzinfo | None = None) -> datetime:
    """Move an aware datetime into `timezone` and drop the zone."""
    if timezone is None:
        timezone = get_current_timezone()
    if is_naive(value):
        raise ValueError("make_naive expects an aware datetime")
    return value.astimezone(timezone).replace(tzinfo=None)


def localtime(value: datetime | None = None, timezone: tzinfo | None = None) -> datetime:
    """`value` as it reads in `timezone`, defaulting to now and to the current one."""
    if timezone is None:
        timezone = get_current_timezone()
    if value is None:
        value = now()
    if is_naive(value):
        raise ValueError("localtime expects an aware datetime")
    return value.astimezone(timezone)
