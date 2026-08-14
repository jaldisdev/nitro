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

"""The JSON encoder and decoder the framework uses.

`orjson` rather than the standard library, because a response body is encoded on
every request that returns one and the difference is most of what building that
response costs. What it produces is bytes, which is what a response body is, so
the encode step that used to follow is gone too.

Two differences from the standard library are worth knowing, and both are
answers this framework would give anyway: keys that are not strings are coerced
rather than refused, and `NaN` and infinities raise instead of being written as
JavaScript literals that no JSON parser accepts.
"""

from __future__ import annotations

from typing import Any

import orjson

#: Raised by :func:`loads` for a body that is not JSON. A subclass of the
#: standard library's, so anything already catching that still catches this.
JSONDecodeError = orjson.JSONDecodeError

_OPTIONS = orjson.OPT_NON_STR_KEYS


def dumps(value: Any) -> bytes:
    """`value` as JSON, encoded."""
    return orjson.dumps(value, option=_OPTIONS)


def dumps_str(value: Any) -> str:
    """`value` as JSON, for the places that carry text rather than bytes."""
    return orjson.dumps(value, option=_OPTIONS).decode()


def loads(data: bytes | str) -> Any:
    """What `data` decodes to."""
    return orjson.loads(data)
