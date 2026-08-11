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

"""Mounting a sub-table under a path prefix.

A mount is a grouping device, not a hosting one: it takes a list of route
declarations and re-registers them under a prefix in the parent. There is no
notion of handing a request off to a separate application — everything a Nitro
project serves is served by the same application object.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from nitro.routing.router import Router

__all__ = ["Mount"]


class Mount:
    """Route declarations, to be attached under `prefix`.

    `name` becomes a namespace: a route named ``status`` inside
    ``Mount("/api", ..., name="api")`` reverses as ``api:status``.
    """

    def __init__(self, prefix: str, routes: Router | Iterable[Any], *, name: str | None = None):
        if not prefix.startswith("/"):
            raise ValueError(f"mount prefix must start with '/', got {prefix!r}")
        # A trailing slash would double up against the mounted paths, which
        # start with one of their own.
        self.prefix = prefix.rstrip("/")
        self.name = name
        self.routes: list[Any] = list(routes.routes if isinstance(routes, Router) else routes)

    def attach(self, router: Router, prefix: str = "", namespace: str | None = None) -> None:
        """Register everything this mount holds on `router`, under its prefix."""
        from nitro.routing.patterns import qualified

        inner = qualified(self.name, namespace) or self.name or namespace
        for entry in self.routes:
            attach = getattr(entry, "attach", None)
            if attach is None:
                raise TypeError(
                    f"{entry!r} is not a route; expected an HTTPRoute, WebSocketRoute, "
                    "WebTransportRoute or Mount"
                )
            attach(router, f"{prefix}{self.prefix}", inner)

    def __len__(self) -> int:
        return len(self.routes)

    def __repr__(self) -> str:
        return f"Mount({self.prefix!r}, routes={len(self.routes)})"
