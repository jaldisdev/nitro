import zoneinfo
from datetime import UTC, datetime

from nitro.settings import settings


###
# Time zones
###

def get_default_timezone() -> str:
    """
    Return the default time zone as a tzinfo instance.
    
    This is the time zone defined by settings.TIME_ZONE.
    """
    return zoneinfo.ZoneInfo(settings.TIME_ZONE)


def get_current_timezone() -> str:
    """Return the currently active time zone as a tzinfo instance."""
    return getattr(_active, 'value', get_default_timezone())


def get_current_timezone_name() -> str:
    """Return the name of the currently active time zone."""
    return _get_timezone_name(get_current_timezone())


def _get_timezone_name(timezone: str) -> str:
    """
    Return the offset for fixed offset timezones, or the name of timezone if
    not set.
    """
    return timezone.tzname(None) or str(timezone)


###
# Local time
###

def now() -> datetime:
    """
    Return an aware or naive datetime.datetime, depending on settings.USE_TZ.
    """
    return datetime.now(tz=UTC if settings.USE_TZ else None)


def is_aware(value: datetime) -> bool:
    """
    Determine if a given datetime.datetime is aware.
    
    The concept is defined in Python's docs:
    https://docs.python.org/library/datetime.html#datetime.tzinfo
    
    Assuming value.tzinfo is either None or a proper datetime.tzinfo,
    value.utcoffset() implements the appropriate logic.
    """
    return value.utcoffset() is not None


def is_naive(value: datetime) -> bool:
    """
    Determine if a given datetime.datetime is naive.
    
    The concept is defined in Python's docs:
    https://docs.python.org/library/datetime.html#datetime.tzinfo
    
    Assuming value.tzinfo is either None or a proper datetime.tzinfo,
    value.utcoffset() implements the appropriate logic.
    """
    return value.utcoffset() is None


def make_aware(value: datetime, timezone: str|None = None) -> datetime:
    """Make a naive datetime.datetime in a given time zone aware."""
    if timezone is None:
        timezone = get_current_timezone()
    # Check that we won't overwrite the timezone of an aware datetime.
    if is_aware(value):
        raise ValueError(f'make_aware expects a naive datetime, got {value}')
    # This may be wrong around DST changes!
    return value.replace(tzinfo=timezone)


def make_naive(value: datetime, timezone: str|None = None) -> datetime:
    """Make an aware datetime.datetime naive in a given time zone."""
    if timezone is None:
        timezone = get_current_timezone()
    # Emulate the behavior of astimezone() on Python < 3.6.
    if is_naive(value):
        raise ValueError("make_naive() cannot be applied to a naive datetime")
    return value.astimezone(timezone).replace(tzinfo=None)
