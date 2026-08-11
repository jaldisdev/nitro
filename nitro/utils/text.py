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

"""String helpers, for application code."""

from __future__ import annotations

import re

__all__ = ["capitalize_first", "lower_first", "to_camel_case", "to_snake_case"]

#: An underscore and the character it introduces, which camel case joins up.
_UNDERSCORED = re.compile(r"_+([a-zA-Z0-9])")

#: The gap before an inner capital, where snake case takes a underscore.
_BEFORE_CAPITAL = re.compile(r"(?<!^)(?=[A-Z])")


def capitalize_first(text: str) -> str:
    """`text` with its first letter upper case, leaving the rest alone.

    Unlike `str.capitalize`, which lowers everything after the first letter.
    """
    if not text:
        return text
    return text[:1].upper() + text[1:]


def lower_first(text: str) -> str:
    """`text` with its first letter lower case, leaving the rest alone."""
    if not text:
        return text
    return text[:1].lower() + text[1:]


def to_camel_case(text: str, capitalize: bool = False) -> str:
    """`text` in camel case: ``user_id`` becomes ``userId``.

    With `capitalize`, the first letter is upper case too — ``UserId``, which
    is usually called Pascal case.
    """
    if not text:
        return text
    converted = _UNDERSCORED.sub(lambda match: match.group(1).upper(), text)
    return capitalize_first(converted) if capitalize else converted


def to_snake_case(text: str) -> str:
    """`text` in snake case: ``userId`` becomes ``user_id``."""
    return _BEFORE_CAPITAL.sub("_", text).lower()
