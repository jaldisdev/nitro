"""Serving files from a directory.

    patterns = [
        HTTPRoute("/static/<path:path>", StaticFiles(directory="static"), name="static"),
    ]

It is an ordinary handler rather than anything the router knows about, so it
sits in the route table beside everything else and the path it serves is the
one its route captures.
"""

from __future__ import annotations

import os
import stat
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

from nitro.protocols.exceptions import Http404, HttpForbidden, HttpMethodNotAllowed
from nitro.protocols.http import FileResponse, HttpRequest, HttpResponse

__all__ = ["StaticFiles"]

_READ_METHODS = ("GET", "HEAD")


class StaticFiles:
    """Files under `directory`, addressed by a route's path parameter.

    With `html`, a directory is served by its `index.html` and a miss by a
    `404.html` if there is one, which is what a single-page application needs.

    `follow_symlink` decides whether a link may point outside `directory`. It is
    off by default: a link out of the served directory is usually an accident,
    and honouring it would serve whatever it happens to point at.
    """

    def __init__(
        self,
        directory: str | Path,
        html: bool = False,
        check_dir: bool = True,
        follow_symlink: bool = False,
    ):
        self.directory = Path(directory).resolve()
        self.html = html
        self.follow_symlink = follow_symlink

        if check_dir and not self.directory.is_dir():
            raise RuntimeError(f"the static directory {self.directory} does not exist")

    async def __call__(self, request: HttpRequest, **parameters: str) -> HttpResponse:
        if request.method not in _READ_METHODS:
            raise HttpMethodNotAllowed(
                detail=f"Method {request.method} not allowed",
                headers={"Allow": ", ".join(_READ_METHODS)},
            )

        try:
            return self._respond(request, self._requested(parameters))
        except PermissionError:
            # The file is there and the server may not read it. That is the
            # server's problem to fix, and nothing the client can authenticate
            # its way past.
            raise HttpForbidden() from None

    def _requested(self, parameters: dict[str, str]) -> str:
        """The path asked for, from the route's captured parameter."""
        if len(parameters) != 1:
            raise TypeError(
                "StaticFiles is routed with exactly one path parameter, as in "
                f"'/static/<path:path>'; this route captured {sorted(parameters)}"
            )
        return next(iter(parameters.values()))

    def _respond(self, request: HttpRequest, requested: str) -> HttpResponse:
        path = self._resolve(requested)

        if path is not None and path.is_dir():
            index = self._resolve(f"{requested.rstrip('/')}/index.html") if self.html else None
            if index is None:
                # Listing a directory would publish names nothing asked to
                # publish, so a directory is refused rather than described.
                raise HttpForbidden()
            path = index

        if path is None:
            fallback = self._resolve("404.html") if self.html else None
            if fallback is None:
                raise Http404()
            return FileResponse(fallback, status_code=404, headers=self._headers(fallback))

        headers = self._headers(path)
        if self._unchanged(request, path):
            return HttpResponse(None, status_code=304, headers=headers)
        return FileResponse(path, headers=headers)

    def _resolve(self, requested: str) -> Path | None:
        """`requested` as a path inside the served directory, or `None`.

        Containment is checked twice and for different reasons: lexically, so a
        `..` cannot climb out of the directory, and again after resolving, so a
        symlink cannot point out of it either. The `path` converter matches
        separators, which is what makes both reachable from a URL.
        """
        candidate = Path(os.path.normpath(self.directory / requested.lstrip("/")))
        if not candidate.is_relative_to(self.directory):
            return None

        if not self.follow_symlink and not candidate.resolve().is_relative_to(self.directory):
            return None

        return candidate if candidate.exists() else None

    def _headers(self, path: Path) -> dict[str, str]:
        details = path.stat()
        if not stat.S_ISREG(details.st_mode):
            return {}
        return {
            "last-modified": formatdate(details.st_mtime, usegmt=True),
            "etag": _etag(details),
            "cache-control": "public, max-age=3600",
        }

    def _unchanged(self, request: HttpRequest, path: Path) -> bool:
        """Whether the client already holds this version of the file."""
        details = path.stat()

        # Checked first and on its own: an entity tag is exact, where a
        # timestamp is only as precise as the filesystem records.
        offered = request.headers.get("if-none-match")
        if offered is not None:
            return _etag(details) in {tag.strip() for tag in offered.split(",")}

        since = request.headers.get("if-modified-since")
        if since is None:
            return False
        try:
            held = parsedate_to_datetime(since)
        except (TypeError, ValueError):
            # An unparseable date is treated as no date at all, which costs a
            # transfer rather than serving something stale.
            return False
        return parsedate_to_datetime(formatdate(details.st_mtime, usegmt=True)) <= held


def _etag(details: os.stat_result) -> str:
    return f'"{details.st_mtime}-{details.st_size}"'
