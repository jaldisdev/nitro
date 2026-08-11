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
from nitro.protocols.exceptions import HttpException
from nitro.protocols.http import HttpRequest, HttpResponse
from nitro.protocols.websocket import WebSocket

__all__ = [
    "CORSMiddleware",
    "ExceptionMiddleware",
    "LoggingMiddleware",
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

    async def __call__(
        self, connection: Any, call_next: Callable[[Any], Awaitable[Any]]
    ) -> Any:
        protocol = _protocol_of(connection)
        path = getattr(connection, "path", "unknown")

        started = time.monotonic()
        logger.info("%s started: %s", protocol, path)

        try:
            result = await call_next(connection)
        except Exception:
            logger.exception(
                "%s failed: %s (%.3fs)", protocol, path, time.monotonic() - started
            )
            raise

        logger.info(
            "%s completed: %s (%.3fs)", protocol, path, time.monotonic() - started
        )
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
        recent = [
            stamp for stamp in self.requests.get(client, ()) if now - stamp < self.window
        ]

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

    async def _registered(
        self, request: HttpRequest, exception: BaseException
    ) -> tuple[bool, Any]:
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
                routes=[route.path for route in router] if router is not None else (),
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
