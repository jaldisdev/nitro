"""The middleware contract.

A middleware wraps a handler. It may implement one hook per protocol —
``__http__``, ``__websocket__``, ``__webtransport__`` — or a single ``__call__``
that answers for all three.

Which hooks a middleware implements is read off its class by comparing the
attribute with the one this class defines. A hook is never called to find out
whether it exists: doing that makes an error raised *inside* a middleware
indistinguishable from the middleware not being there, and the middleware is
then skipped for every connection with nothing said about it.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Final

from nitro.protocols.http import HttpRequest, HttpResponse
from nitro.protocols.websocket import WebSocket
from nitro.protocols.webtransport import WebTransportSession

__all__ = ["PROTOCOL_HOOKS", "UNIVERSAL_HOOK", "Middleware", "MiddlewareProtocol"]


class MiddlewareProtocol:
    """The protocols a middleware may answer for."""

    HTTP: Final = "http"
    WEBSOCKET: Final = "websocket"
    WEBTRANSPORT: Final = "webtransport"


#: The hook each protocol looks for. Named here rather than built from the
#: protocol name, so the set of hooks is something that can be read.
PROTOCOL_HOOKS: Final[dict[str, str]] = {
    MiddlewareProtocol.HTTP: "__http__",
    MiddlewareProtocol.WEBSOCKET: "__websocket__",
    MiddlewareProtocol.WEBTRANSPORT: "__webtransport__",
}

#: The hook that answers for any protocol a middleware has no specific one for.
UNIVERSAL_HOOK: Final = "__call__"


class Middleware(ABC):
    """Base class for middleware.

    Implement whichever of the protocol hooks apply. A middleware that does not
    implement one is left out of that protocol's chain rather than having to
    pass the connection through itself.

        class TimingMiddleware(Middleware):
            async def __http__(self, request, call_next):
                started = time.perf_counter()
                response = await call_next(request)
                elapsed = time.perf_counter() - started
                logger.info("%s took %.3fs", request.path, elapsed)
                return response
    """

    def __init__(self, app: Any | None = None) -> None:
        self.app = app

    async def __aenter__(self) -> Middleware:
        """Entered once per connection, around this middleware's hook."""
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Left when this middleware's hook has returned or raised.

        Returning `True` suppresses the exception, as it does for any context
        manager; returning `None` lets it carry on to the layer outside.
        """
        return None

    async def __http__(
        self,
        request: HttpRequest,
        call_next: Callable[[HttpRequest], Awaitable[HttpResponse]],
    ) -> HttpResponse:
        """Handle one HTTP request. Optional."""
        raise NotImplementedError(f"{type(self).__name__} does not implement __http__")

    async def __websocket__(
        self,
        websocket: WebSocket,
        call_next: Callable[[WebSocket], Awaitable[None]],
    ) -> None:
        """Handle one WebSocket connection. Optional."""
        raise NotImplementedError(f"{type(self).__name__} does not implement __websocket__")

    async def __webtransport__(
        self,
        session: WebTransportSession,
        call_next: Callable[[WebTransportSession], Awaitable[None]],
    ) -> None:
        """Handle one WebTransport session. Optional."""
        raise NotImplementedError(f"{type(self).__name__} does not implement __webtransport__")

    async def __call__(
        self,
        connection: Any,
        call_next: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """Handle a connection of any protocol. Optional.

        Used for the protocols this middleware has no specific hook for, so one
        implementation can cover all three.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement __call__")

    @classmethod
    def implements(cls, hook: str) -> bool:
        """Whether `cls` provides `hook` rather than inheriting the base one."""
        return getattr(cls, hook, None) is not getattr(Middleware, hook, None)

    def has_protocol_support(self, protocol: str) -> bool:
        """Whether this middleware answers for `protocol`."""
        try:
            hook = PROTOCOL_HOOKS[protocol]
        except KeyError:
            known = ", ".join(sorted(PROTOCOL_HOOKS))
            raise ValueError(f"{protocol!r} is not a protocol; expected one of {known}") from None

        middleware_class = type(self)
        return middleware_class.implements(hook) or middleware_class.implements(UNIVERSAL_HOOK)
