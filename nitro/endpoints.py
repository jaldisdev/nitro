from collections.abc import Callable, Sequence
from typing import Any, Literal

from nitro.di import DependencyCache, cache_for, dependencies_for, resolve_dependencies
from nitro.protocols.exceptions import HttpMethodNotAllowed
from nitro.protocols.http import HttpRequest, HttpResponse
from nitro.protocols.websocket import WebSocket, WebSocketDisconnect
from nitro.protocols.webtransport import WebTransportDisconnect, WebTransportSession


async def _supplied(
    function: Callable[..., Any],
    context: Any,
    cache: DependencyCache | None = None,
) -> dict[str, Any]:
    """Values for the parameters `function` wants injected, or nothing.

    `cache` spans one request or one connection. Sharing it across the hooks
    of a socket is what makes a dependency they have in common resolve once
    for the connection rather than once per hook.
    """
    graph = dependencies_for(function)
    if not graph:
        return {}
    # `is None` rather than `or`: an empty cache is falsy, so a cache that has
    # resolved nothing yet would be thrown away and replaced here — which is
    # every cache, the first time it is used.
    return await resolve_dependencies(
        graph, context, DependencyCache() if cache is None else cache
    )


#: The verbs an endpoint may implement, lower case as they are written on the
#: class. Routing reads this to work out what an endpoint class answers.
HTTP_METHODS: tuple[str, ...] = (
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
)


class HTTPEndpoint:
    """
    Base class for HTTP endpoints with method-based dispatch.
    """

    #: Decorators wrapped around this endpoint, outermost first, so the one
    #: named first is the first to run. Applied once when the route table is
    #: built, and around the whole request rather than one method of it — for a
    #: single method, decorate it with `nitro.utils.decorators.method_decorator`.
    decorators: Sequence[Callable[..., Any]] = ()

    async def __call__(self, request: HttpRequest, **params) -> HttpResponse:
        """Dispatch to method handler."""
        return await self.dispatch(request, **params)

    async def dispatch(self, request: HttpRequest, **params) -> HttpResponse:
        """Dispatch request to appropriate method handler."""
        handler = getattr(self, request.method.lower(), None)

        # HEAD falls back to GET per RFC 9110 §9.3.2
        if handler is None and request.method.upper() == "HEAD":
            handler = getattr(self, "get", None)

        if handler is None:
            return await self.method_not_allowed(request)

        # A verb method is reached through here rather than being the
        # registered handler, so its graph is read on first use instead of at
        # registration; `dependencies_for` remembers it per method.
        supplied = await _supplied(handler, request, cache_for(request))
        response = await handler(request, **supplied, **params)

        if request.method.upper() == "HEAD":
            if "content-length" not in response._headers:
                response._headers["content-length"] = str(len(response._body))
            response._body = b""

        return response

    async def method_not_allowed(self, request: HttpRequest) -> HttpResponse:
        """Called when HTTP method is not implemented."""
        allowed = [method.upper() for method in HTTP_METHODS if hasattr(self, method)]
        if "GET" in allowed and "HEAD" not in allowed:
            allowed.insert(allowed.index("GET") + 1, "HEAD")
        raise HttpMethodNotAllowed(
            detail=f"Method {request.method} not allowed",
            headers={"Allow": ", ".join(allowed)} if allowed else {},
        )


class WebSocketEndpoint:
    """
    Base class for WebSocket endpoints with automatic encoding.
    """

    #: Decorators wrapped around this endpoint, outermost first, so the one
    #: named first is the first to run. Applied once when the route table is
    #: built, and around the whole connection rather than one method of it — for a
    #: single method, decorate it with `nitro.utils.decorators.method_decorator`.
    decorators: Sequence[Callable[..., Any]] = ()

    encoding: Literal["text", "bytes", "json"] = "text"

    async def __call__(self, websocket: WebSocket, **params):
        """Handle WebSocket lifecycle."""
        return await self.dispatch(websocket, **params)

    async def dispatch(self, websocket: WebSocket, **params) -> None:
        """Dispatch WebSocket events to lifecycle hooks.

        Dependencies are resolved once for the connection and supplied to
        every hook that asks for them, rather than per message: a socket is
        one unit of work, and re-resolving per frame would give `on_receive`
        a different value each time.
        """
        shared = cache_for(websocket)
        connect = await _supplied(self.on_connect, websocket, shared)
        receive = await _supplied(self.on_receive, websocket, shared)
        disconnect = await _supplied(self.on_disconnect, websocket, shared)

        await self.on_connect(websocket, **connect, **params)

        close_code = 1000
        try:
            async for data in websocket.iter(self.encoding):
                await self.on_receive(websocket, data, **receive, **params)
        except WebSocketDisconnect as exc:
            close_code = exc.code
        finally:
            await self.on_disconnect(websocket, close_code, **disconnect, **params)

    async def on_connect(self, websocket: WebSocket, **params) -> None:
        """Override to handle WebSocket connection."""
        await websocket.accept()

    async def on_receive(self, websocket: WebSocket, data: Any, **params) -> None:
        """Override to handle incoming messages."""
        pass

    async def on_disconnect(
        self, websocket: WebSocket, close_code: int, **params
    ) -> None:
        """Override to handle disconnection."""
        pass


class WebTransportEndpoint:
    """
    Base class for WebTransport endpoints with support for datagrams and streams.
    """

    #: Decorators wrapped around this endpoint, outermost first, so the one
    #: named first is the first to run. Applied once when the route table is
    #: built, and around the whole session rather than one method of it — for a
    #: single method, decorate it with `nitro.utils.decorators.method_decorator`.
    decorators: Sequence[Callable[..., Any]] = ()

    encoding: Literal["bytes", "text", "json"] = "bytes"
    use_streams: bool = False

    async def __call__(self, session: WebTransportSession, **params):
        """Handle WebTransport lifecycle."""
        return await self.dispatch(session, **params)

    async def dispatch(self, session: WebTransportSession, **params) -> None:
        """Dispatch WebTransport events to lifecycle hooks.

        One cache for the session, as `WebSocketEndpoint.dispatch` does and
        for the same reason.
        """
        shared = cache_for(session)
        connect = await _supplied(self.on_connect, session, shared)
        datagram = await _supplied(self.on_datagram, session, shared)
        stream_supplied = await _supplied(self.on_stream, session, shared)
        disconnect = await _supplied(self.on_disconnect, session, shared)

        await self.on_connect(session, **connect, **params)

        close_code = 0
        try:
            if self.use_streams:
                import asyncio

                async def handle_datagrams():
                    async for data in session.iter_datagrams(self.encoding):
                        await self.on_datagram(session, data, **datagram, **params)

                async def handle_streams():
                    while True:
                        stream = await session.receive_stream()
                        asyncio.create_task(
                            self.on_stream(session, stream, **stream_supplied, **params)
                        )

                await asyncio.gather(
                    handle_datagrams(), handle_streams(), return_exceptions=True
                )
            else:
                async for data in session.iter_datagrams(self.encoding):
                    await self.on_datagram(session, data, **datagram, **params)
        except WebTransportDisconnect as exc:
            close_code = exc.code
        finally:
            await self.on_disconnect(session, close_code, **disconnect, **params)

    async def on_connect(self, session: WebTransportSession, **params) -> None:
        """Override to handle session connection."""
        await session.accept()

    async def on_datagram(
        self, session: WebTransportSession, data: Any, **params
    ) -> None:
        """Override to handle incoming datagrams."""
        pass

    async def on_stream(
        self, session: WebTransportSession, stream: Any, **params
    ) -> None:
        """Override to handle incoming streams (only called if use_streams=True)."""
        pass

    async def on_disconnect(
        self, session: WebTransportSession, close_code: int, **params
    ) -> None:
        """Override to handle disconnection."""
        pass
