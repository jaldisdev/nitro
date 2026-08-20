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

"""Header values that are fiddlier than they look.

Both helpers here exist because the obvious one-line version is wrong in a way
that only shows up later: an f-string `Vary` overwrites what an inner layer
already asked to vary on, and an f-string `Content-Disposition` corrupts every
filename that is not plain ASCII.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from nitro.protocols.http import HttpResponse

__all__ = ["content_disposition_header", "patch_vary_headers"]

_DELIMITER = re.compile(r"\s*,\s*")


def content_disposition_header(as_attachment: bool, filename: str | None) -> str | None:
    """Build a ``Content-Disposition`` value, per RFC 6266.

    Returns `None` when there is nothing to say — an inline response with no
    name of its own does not need the header at all.

    A filename outside ASCII cannot go in the quoted `filename` form, because
    the header is latin-1 on the wire and the name would arrive mangled. Such a
    name is percent-encoded into the `filename*` form instead, which is what
    makes ``report-Ω.pdf`` survive the trip.
    """
    if not filename:
        return "attachment" if as_attachment else None

    disposition = "attachment" if as_attachment else "inline"
    try:
        filename.encode("ascii")
    except UnicodeEncodeError:
        expression = f"filename*=utf-8''{quote(filename)}"
    else:
        escaped = filename.replace("\\", "\\\\").replace('"', '\\"')
        expression = f'filename="{escaped}"'
    return f"{disposition}; {expression}"


def patch_vary_headers(response: HttpResponse, additional_headers: Sequence[str]) -> None:
    """Add `additional_headers` to `response`'s ``Vary``, keeping what is there.

    Middleware layers each know one thing the response varies on and none of
    them knows the others, so this merges rather than assigns. Order is
    preserved because some caches key on the header verbatim, and a name
    already listed is not repeated — the comparison is case-insensitive, since
    ``Accept-Encoding`` and ``accept-encoding`` are the same header.
    """
    headers = response.headers
    existing_key = next((key for key in headers if key.lower() == "vary"), None)

    current = headers[existing_key] if existing_key is not None else ""
    vary = [header for header in _DELIMITER.split(current) if header]

    seen = {header.lower() for header in vary}
    for header in additional_headers:
        if header.lower() not in seen:
            seen.add(header.lower())
            vary.append(header)

    headers[existing_key or "vary"] = "*" if "*" in vary else ", ".join(vary)
