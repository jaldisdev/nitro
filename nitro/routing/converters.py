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

"""Path converters.

A converter decides two things about a path parameter: the shape of the text it
accepts, and what Python value that text becomes. Only the first crosses into
the compiled matcher — as an expression — so a converter can do whatever it
likes in :meth:`Converter.to_python` without the matcher needing to know.
"""

from __future__ import annotations

import uuid
from typing import Any

__all__ = [
    "Converter",
    "IntConverter",
    "PathConverter",
    "SlugConverter",
    "StringConverter",
    "UUIDConverter",
    "converter_for",
    "get_converters",
    "register_converter",
]


class Converter:
    """Base class for path converters."""

    #: Expression the captured text must match in full.
    regex: str = "[^/]+"

    #: Whether the parameter may span ``/``. A converter whose expression can
    #: match a separator must say so, otherwise the matcher will never offer it
    #: text containing one.
    spans_separators: bool = False

    def to_python(self, value: str) -> Any:
        """Turn the captured text into a Python value."""
        return value

    def to_url(self, value: Any) -> str:
        """Turn a Python value back into path text."""
        return str(value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(regex={self.regex!r})"


class StringConverter(Converter):
    """Any non-empty run of characters other than ``/``."""

    regex = "[^/]+"


class IntConverter(Converter):
    """A non-negative integer."""

    regex = "[0-9]+"

    def to_python(self, value: str) -> int:
        return int(value)


class SlugConverter(Converter):
    """Letters, digits, hyphens and underscores."""

    regex = "[-a-zA-Z0-9_]+"


class UUIDConverter(Converter):
    """A UUID in its canonical hyphenated form."""

    regex = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

    def to_python(self, value: str) -> uuid.UUID:
        return uuid.UUID(value)


class PathConverter(Converter):
    """Any non-empty text, including ``/``. Only valid at the end of a path."""

    regex = ".+"
    spans_separators = True


_CONVERTERS: dict[str, type[Converter]] = {
    "str": StringConverter,
    "int": IntConverter,
    "slug": SlugConverter,
    "uuid": UUIDConverter,
    "path": PathConverter,
}


def register_converter(name: str, converter: type[Converter]) -> None:
    """Make `converter` available as ``<name:parameter>`` in a path."""
    if not isinstance(converter, type) or not issubclass(converter, Converter):
        raise TypeError(f"converter for {name!r} must be a Converter subclass")
    _CONVERTERS[name] = converter


def get_converters() -> dict[str, type[Converter]]:
    """Every registered converter, by name."""
    return dict(_CONVERTERS)


def converter_for(name: str) -> Converter:
    """The converter registered as `name`.

    ``regex("...")`` is understood directly and produces a converter that
    accepts exactly that expression and leaves the value as text.
    """
    inline = _inline_expression(name)
    if inline is not None:
        return _InlineConverter(inline)

    try:
        return _CONVERTERS[name]()
    except KeyError:
        known = ", ".join(sorted(_CONVERTERS))
        raise LookupError(
            f"unknown path converter {name!r}; known converters are {known}"
        ) from None


def _inline_expression(name: str) -> str | None:
    """The expression from a ``regex("...")`` declaration, if that is what it is."""
    if not name.startswith("regex(") or not name.endswith(")"):
        return None

    body = name[len("regex(") : -1].strip()
    if len(body) >= 2 and body[0] == body[-1] and body[0] in "\"'":
        return body[1:-1]
    raise ValueError(f"the expression in {name!r} must be quoted")


class _InlineConverter(Converter):
    """A converter declared inline as ``regex("...")``."""

    def __init__(self, expression: str) -> None:
        self.regex = expression
        # An inline expression is written by whoever uses it, and only they know
        # whether it is meant to span separators. Assuming it does not is the
        # safe reading: a parameter that stops at the next `/` cannot swallow
        # the rest of the path by accident.
        self.spans_separators = False

    def __repr__(self) -> str:
        return f"regex({self.regex!r})"
