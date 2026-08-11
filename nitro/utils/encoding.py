import datetime
from decimal import Decimal
from types import NoneType


class NitroUnicodeDecodeError(UnicodeDecodeError):
    def __str__(self):
        return f"{super().__str__()}. You passed in {self.object!r} ({type(self.object)})"


_PROTECTED_TYPES = (
    NoneType,
    int,
    float,
    Decimal,
    datetime.datetime,
    datetime.date,
    datetime.time,
)


def is_protected_type(obj):
    """Determine if the object instance is of a protected type.

    Objects of protected types are preserved as-is when passed to
    force_str(strings_only=True).
    """
    return isinstance(obj, _PROTECTED_TYPES)


def ensure_str(s, encoding="utf-8", strings_only=False, errors="strict"):
    """
    Return a string representing 's'. Treat bytestrings using the 'encoding'
    codec.

    If strings_only is True, don't convert (some) non-string-like objects.
    """
    # Handle the common case first for performance reasons.
    if issubclass(type(s), str):
        return s
    if strings_only and is_protected_type(s):
        return s
    try:
        s = str(s, encoding, errors) if isinstance(s, bytes) else str(s)
    except UnicodeDecodeError as e:
        raise NitroUnicodeDecodeError(*e.args) from None
    return s


def ensure_bytes(s, encoding="utf-8", strings_only=False, errors="strict"):
    """
    Return a bytestring version of 's', encoded as specified in 'encoding'.

    If strings_only is True, don't convert (some) non-string-like objects.
    """
    # Handle the common case first for performance reasons.
    if isinstance(s, bytes):
        if encoding == "utf-8":
            return s
        else:
            return s.decode("utf-8", errors).encode(encoding, errors)
    if strings_only and is_protected_type(s):
        return s
    if isinstance(s, memoryview):
        return bytes(s)
    return str(s).encode(encoding, errors)
