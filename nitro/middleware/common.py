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

"""The middleware that ships with Nitro.

Each one is opt-in through the ``MIDDLEWARE`` setting; none is installed by
default.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from nitro.middleware.base import Middleware
from nitro.protocols.exceptions import HttpException, HttpForbidden
from nitro.protocols.http import HttpRequest, HttpResponse
from nitro.protocols.websocket import WebSocket

__all__ = [
    "CORSMiddleware",
    "ExceptionMiddleware",
    "LoggingMiddleware",
    "OriginMiddleware",
    "RateLimitMiddleware",
    "SecurityHeadersMiddleware",
]

logger = logging.getLogger("nitro.middleware")

HttpNext = Callable[[HttpRequest], Awaitable[Any]]


def _protocol_of(connection: Any) -> str:
    """Which protocol `connection` arrived over.

    Read from the scope the server built, where it is an attribute rather than
    a key: the scope is a compiled object, and asking it for an item raises.
    """
    return getattr(getattr(connection, "scope", None), "proto", "unknown")


class LoggingMiddleware(Middleware):
    """Logs every connection and how long it was held, for every protocol."""

    async def __call__(self, connection: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        protocol = _protocol_of(connection)
        path = getattr(connection, "path", "unknown")

        started = time.monotonic()
        logger.info("%s started: %s", protocol, path)

        try:
            result = await call_next(connection)
        except HttpException as exception:
            # Raising one of these names the answer the handler wants, so below
            # 500 it is an outcome and a traceback for it is noise. A 5xx did go
            # wrong whichever way it was raised, and keeps its traceback.
            held = time.monotonic() - started
            if exception.status_code >= 500:
                logger.exception("%s failed: %s (%.3fs)", protocol, path, held)
            else:
                logger.info(
                    "%s answered %d: %s (%.3fs)", protocol, exception.status_code, path, held
                )
            raise
        except Exception:
            logger.exception("%s failed: %s (%.3fs)", protocol, path, time.monotonic() - started)
            raise

        logger.info("%s completed: %s (%.3fs)", protocol, path, time.monotonic() - started)
        return result


class CORSMiddleware(Middleware):
    """Cross-origin headers, including the preflight answer.

    Configured with the ``CORS_*`` settings.
    """

    def __init__(self, app: Any | None = None) -> None:
        super().__init__(app)

        from nitro.settings import settings

        self.allow_origins: list[str] = list(settings.CORS_ALLOWED_ORIGINS)
        self.allow_all: bool = bool(settings.CORS_ALLOW_ALL_ORIGINS)
        self.allow_credentials: bool = bool(settings.CORS_ALLOW_CREDENTIALS)
        self.allow_methods: list[str] = list(settings.CORS_ALLOW_METHODS)
        self.allow_headers: list[str] = list(settings.CORS_ALLOW_HEADERS)

    async def __http__(self, request: HttpRequest, call_next: HttpNext) -> Any:
        if request.method == "OPTIONS":
            return self._preflight(request)

        response = await call_next(request)
        if not isinstance(response, HttpResponse):
            # The handler answered through the protocol itself, so there are no
            # headers left here to add to.
            return response

        origin = request.headers.get("origin")
        if origin and self._allows(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            if self.allow_credentials:
                response.headers["Access-Control-Allow-Credentials"] = "true"

        return response

    def _allows(self, origin: str) -> bool:
        return self.allow_all or origin in self.allow_origins

    def _preflight(self, request: HttpRequest) -> HttpResponse:
        headers: dict[str, str] = {}

        origin = request.headers.get("origin")
        if origin and self._allows(origin):
            headers["Access-Control-Allow-Origin"] = origin
            if self.allow_credentials:
                headers["Access-Control-Allow-Credentials"] = "true"
            headers["Access-Control-Allow-Methods"] = ", ".join(self.allow_methods)
            headers["Access-Control-Allow-Headers"] = ", ".join(self.allow_headers)
            headers["Access-Control-Max-Age"] = "600"

        return HttpResponse(status_code=200, headers=headers)


class RateLimitMiddleware(Middleware):
    """Refuses with 429 past a per-client limit.

    The count is held in this process, so with several workers the effective
    limit is this one multiplied by the number of them. It is a blunt guard
    against one client flooding a worker, not a distributed quota.
    """

    def __init__(self, app: Any | None = None) -> None:
        super().__init__(app)
        self.requests: dict[str, list[float]] = {}
        self.max_requests = 100
        self.window = 60

    async def __aenter__(self) -> RateLimitMiddleware:
        self._forget_expired()
        return self

    def _forget_expired(self) -> None:
        now = time.monotonic()
        for client in list(self.requests):
            recent = [stamp for stamp in self.requests[client] if now - stamp < self.window]
            if recent:
                self.requests[client] = recent
            else:
                del self.requests[client]

    def _within_limit(self, client: str) -> bool:
        now = time.monotonic()
        recent = [stamp for stamp in self.requests.get(client, ()) if now - stamp < self.window]

        if len(recent) >= self.max_requests:
            self.requests[client] = recent
            return False

        recent.append(now)
        self.requests[client] = recent
        return True

    async def __http__(self, request: HttpRequest, call_next: HttpNext) -> Any:
        client = request.client.host if request.client else "unknown"

        if not self._within_limit(client):
            return HttpResponse(
                content={"error": "Rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(self.window)},
            )

        return await call_next(request)

    async def __websocket__(
        self, websocket: WebSocket, call_next: Callable[[WebSocket], Awaitable[None]]
    ) -> None:
        client = websocket.client.host if websocket.client else "unknown"

        if not self._within_limit(client):
            await websocket.close(code=1008, reason="Rate limit exceeded")
            return

        await call_next(websocket)


class ExceptionMiddleware(Middleware):
    """Turns an exception into the answer the client is owed.

    An `HttpException` is an answer rather than a failure — raising `Http404`
    gives a 404, not a 500 with a 404 buried in the log.
    """

    async def __http__(self, request: HttpRequest, call_next: HttpNext) -> Any:
        try:
            return await call_next(request)
        except HttpException as exception:
            handled, answer = await self._registered(request, exception)
            if handled:
                return answer
            page = self._debug_page(request, exception.status_code, exception)
            return page or exception.as_response()
        except Exception as exception:
            logger.exception("unhandled exception in the handler for %s", request.path)

            handled, answer = await self._registered(request, exception)
            if handled:
                return answer

            page = self._debug_page(request, 500, exception)
            if page is not None:
                return page
            return HttpResponse(content={"error": "Internal server error"}, status_code=500)

    async def _registered(self, request: HttpRequest, exception: BaseException) -> tuple[bool, Any]:
        """The application's own handler for `exception`, run here.

        This middleware catches before the application does, so without asking
        the same registry a project's handlers would be reachable only when
        this middleware is not installed.
        """
        dispatch = getattr(self.app, "_dispatch_exception", None)
        if dispatch is None:
            return False, None
        return await dispatch(request, exception)

    def _debug_page(
        self, request: HttpRequest, status_code: int, exception: BaseException
    ) -> HttpResponse | None:
        """The same page the application shows, so both routes agree."""
        from nitro.settings import settings
        from nitro.views.debug import debug_response

        router = getattr(self.app, "router", None)
        # The application's own answer when there is one, since it may have been
        # told directly rather than taking the setting.
        debug = getattr(self.app, "debug", None)
        query = request.url.query
        try:
            return debug_response(
                status_code,
                request.method,
                f"{request.path}?{query}" if query else request.path,
                debug=bool(settings.DEBUG) if debug is None else debug,
                exception=exception,
                routes=router if router is not None else (),
            )
        except Exception:
            logger.exception("the debug page for %s could not be rendered", status_code)
            return None

    async def __websocket__(
        self, websocket: WebSocket, call_next: Callable[[WebSocket], Awaitable[None]]
    ) -> None:
        try:
            await call_next(websocket)
        except Exception:
            logger.exception("unhandled exception in the handler for %s", websocket.path)
            try:
                await websocket.close(code=1011, reason="Internal server error")
            except RuntimeError:
                logger.debug("the WebSocket was already closed", exc_info=True)


class SecurityHeadersMiddleware(Middleware):
    """Adds the security headers named by the ``SECURE_*`` settings."""

    def __init__(self, app: Any | None = None) -> None:
        super().__init__(app)

        from nitro.settings import settings

        self.hsts_seconds: int = settings.SECURE_HSTS_SECONDS
        self.hsts_include_subdomains: bool = settings.SECURE_HSTS_INCLUDE_SUBDOMAINS
        self.content_type_nosniff: bool = settings.SECURE_CONTENT_TYPE_NOSNIFF
        self.frame_deny: bool = settings.SECURE_FRAME_DENY

    async def __http__(self, request: HttpRequest, call_next: HttpNext) -> Any:
        response = await call_next(request)
        if not isinstance(response, HttpResponse):
            return response

        if self.hsts_seconds > 0:
            value = f"max-age={self.hsts_seconds}"
            if self.hsts_include_subdomains:
                value += "; includeSubDomains"
            response.headers["Strict-Transport-Security"] = value

        if self.content_type_nosniff:
            response.headers["X-Content-Type-Options"] = "nosniff"

        if self.frame_deny:
            response.headers["X-Frame-Options"] = "DENY"

        return response


class OriginMiddleware(Middleware):
    """Refuses a state-changing request that came from somewhere else.

    This is Nitro's answer to cross-site request forgery, and it is deliberately
    not a token scheme. There is no token, no secret in the session, no tag to
    render into a form and no decorator to exempt a view — the whole check is
    these headers, so there is nothing for an author to forget and nothing that
    needs a form framework to carry it.

    ``Sec-Fetch-Site`` is asked first, because the browser computes it and it
    cannot be set by script. ``Origin`` is the fallback for clients that do not
    send it.

    An unsafe method arriving with neither header is refused. Something has to
    give here: a browser sends `Origin` on every unsafe request, so what is left
    is mostly a client that could just as well send one, and treating silence as
    permission would make the check optional for anyone who omits a header.
    Override :meth:`allows` for a caller that genuinely cannot.

    SECURITY WARNING: ``same-site`` is allowed, so a subdomain is trusted. That
    is right for the usual `app.example.test` calling `api.example.test`, and
    wrong when a subdomain is under someone else's control — user content on
    `*.example.test`, say. A deployment like that wants a session-bound token as
    well; neither this check nor `SameSite` can tell the two cases apart.
    """

    #: Methods that do not change anything, and so are not checked. A handler
    #: that changes state on one of these is not made safe by anything here.
    SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

    def __init__(self, app: Any | None = None) -> None:
        super().__init__(app)

        from nitro.settings import settings

        self.allowed_hosts: list[str] = [entry.lower() for entry in settings.ALLOWED_HOSTS]

    def _host_allowed(self, host: str) -> bool:
        """Whether `host` is one this site answers for.

        Mirrors the compiled server's ``ALLOWED_HOSTS`` matching: an empty list
        or ``*`` allows anything, a leading dot covers a domain and everything
        under it, and anything else is an exact name.
        """
        if not self.allowed_hosts:
            return True
        for pattern in self.allowed_hosts:
            if pattern == "*":
                return True
            if pattern.startswith("."):
                if host == pattern[1:] or host.endswith(pattern):
                    return True
            elif host == pattern:
                return True
        return False

    def allows(self, request: HttpRequest, origin: str) -> bool:
        """Whether `origin` may make a state-changing request here.

        `origin` is a serialized origin — ``https://example.test:8443`` — or the
        string ``null``, which a sandboxed frame or a redirect produces and
        which is never allowed.
        """
        if origin == "null":
            return False

        host = origin.partition("://")[2].lower()
        if not host:
            return False

        # The request's own authority first: a request to the site it claims to
        # come from is same-origin whatever the allow list says, which keeps a
        # deployment that never filled ALLOWED_HOSTS in from being wide open
        # here as well.
        #
        # Read off the scope rather than the `Host` header, which HTTP/2 and
        # HTTP/3 do not send — there the authority is a pseudo-header, and a
        # check that went looking for `Host` would find nothing on exactly the
        # versions Nitro is built to serve.
        target = request.url.netloc.lower()
        if target and host == target:
            return True

        return self._host_allowed(host.partition(":")[0])

    async def __http__(self, request: HttpRequest, call_next: HttpNext) -> Any:
        if request.method.upper() in self.SAFE_METHODS:
            return await call_next(request)

        site = request.headers.get("sec-fetch-site")
        if site is not None:
            # `none` is a request the person made themselves — a typed address,
            # a bookmark — which has no originating site to be wrong.
            if site in {"same-origin", "same-site", "none"}:
                return await call_next(request)
            raise HttpForbidden(f"cross-site {request.method} to {request.path}")

        origin = request.headers.get("origin")
        if origin is None:
            raise HttpForbidden(
                f"{request.method} to {request.path} carries neither "
                "Sec-Fetch-Site nor Origin, so where it came from cannot be established"
            )
        if not self.allows(request, origin):
            raise HttpForbidden(f"{request.method} to {request.path} from {origin}")

        return await call_next(request)
