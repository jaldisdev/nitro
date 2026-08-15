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

"""The pages shown in place of a bare status while ``DEBUG`` is on.

A 404 lists the routes that were tried and a 500 shows the traceback, which is
what makes either of them worth reading. Both are built from templates of their
own rather than the project's engine: they have to work when the project's
configuration is what is broken.
"""

from __future__ import annotations

import linecache
import sys
import traceback as tb
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader

from nitro.routing.router import WEBSOCKET_METHOD, WEBTRANSPORT_METHOD

if TYPE_CHECKING:
    from nitro.protocols.http import HttpResponse
    from nitro.routing.router import Route

_TEMPLATES_DIR = Path(__file__).parent / "templates"

#: The statuses a debug page exists for. Anything else keeps its plain answer.
_PAGES = (404, 500)

#: Methods that name a protocol of their own rather than an HTTP verb. They are
#: marked as such on the 404 page: a path listed under one of them cannot be
#: reached by the browser that is reading the page.
_PROTOCOL_METHODS = frozenset({WEBSOCKET_METHOD, WEBTRANSPORT_METHOD})


@dataclass(frozen=True, slots=True)
class RoutePattern:
    """One path on the 404 page, with every method registered for it."""

    path: str
    methods: tuple[str, ...]


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(_TEMPLATES_DIR), autoescape=True)


def debug_response(
    status_code: int,
    method: str,
    path: str,
    *,
    debug: bool,
    exception: BaseException | None = None,
    routes: Iterable[Route] = (),
) -> HttpResponse | None:
    """The debug page for `status_code`, or `None` if there is not one.

    `None` also comes back when `debug` is off, so a caller can ask for the page
    unconditionally and fall back to its plain answer. Whether debugging is on
    is passed in rather than read here: an application may have been told
    directly, and the setting is only where the answer comes from otherwise.
    """
    from nitro.protocols.http import HttpResponse

    if status_code not in _PAGES or not debug:
        return None

    if status_code == 404:
        html = render_404_page(method=method, path=path, url_patterns=route_patterns(routes))
    elif exception is not None:
        html = render_500_page(method=method, path=path, exc=exception)
    else:
        return None

    return HttpResponse(html, status_code=status_code, content_type="text/html; charset=utf-8")


def _extract_frames(exc: BaseException) -> list[dict]:
    frames = []
    trace = tb.TracebackException.from_exception(exc)

    for i, frame in enumerate(trace.stack):
        filename = frame.filename or "<unknown>"
        lineno = frame.lineno or 0
        name = frame.name or "<module>"

        pre_context: list[str] = []
        context_line: str | None = None
        post_context: list[str] = []
        pre_context_lineno = 0

        if filename != "<unknown>" and lineno:
            start = max(1, lineno - 4)
            pre_context_lineno = start
            for line_num in range(start, lineno + 5):
                raw = linecache.getline(filename, line_num)
                if not raw:
                    break
                raw = raw.rstrip("\n")
                if line_num < lineno:
                    pre_context.append(raw)
                elif line_num == lineno:
                    context_line = raw
                else:
                    post_context.append(raw)

        frames.append(
            {
                "id": i,
                "filename": filename,
                "lineno": lineno,
                "name": name,
                "pre_context": pre_context,
                "pre_context_lineno": pre_context_lineno,
                "context_line": context_line,
                "post_context": post_context,
            }
        )

    return frames


def _displayed_methods(methods: list[str]) -> tuple[str, ...]:
    """`methods` as the page shows them.

    HEAD is registered alongside GET for every route that answers one, so
    listing both on every line buries the methods that actually differ.
    """
    if "GET" in methods:
        return tuple(method for method in methods if method != "HEAD")
    return tuple(methods)


def route_patterns(routes: Iterable[Route]) -> list[RoutePattern]:
    """One entry per path, carrying every method registered for it.

    A path may be registered more than once, since HTTP and WebSocket routes
    share a table and a route for each may be declared for the same path.
    Listing it once per registration reads as a duplicate declaration rather
    than as one path serving two protocols.
    """
    methods_by_path: dict[str, list[str]] = {}
    for route in routes:
        methods = methods_by_path.setdefault(route.path, [])
        for method in route.methods:
            if method not in methods:
                methods.append(method)

    return [
        RoutePattern(path, _displayed_methods(methods)) for path, methods in methods_by_path.items()
    ]


def render_404_page(
    method: str,
    path: str,
    url_patterns: list[RoutePattern] | None = None,
) -> str:
    template = _env().get_template("error_404.html")
    return template.render(
        method=method,
        path=path,
        url_patterns=url_patterns or [],
        protocol_methods=_PROTOCOL_METHODS,
    )


def render_500_page(method: str, path: str, exc: BaseException) -> str:
    template = _env().get_template("error_500.html")
    return template.render(
        method=method,
        path=path,
        exception_type=type(exc).__name__,
        exception_value=str(exc),
        frames=_extract_frames(exc),
        python_version=sys.version.split()[0],
    )
