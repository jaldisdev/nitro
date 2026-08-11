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

import asyncio
import json as json_module
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, Literal

from nitro.di import DependencyCache, cache_for, dependencies_for, resolve_dependencies
from nitro.protocols.exceptions import HttpMethodNotAllowed
from nitro.protocols.http import HttpRequest, HttpResponse
from nitro.protocols.websocket import WebSocket, WebSocketDisconnect
from nitro.protocols.webtransport import WebTransportDisconnect, WebTransportSession

logger = logging.getLogger("nitro.endpoints")

#: How a message is handed to `on_receive` and `on_datagram`.
Encoding = Literal["text", "bytes", "json"]


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
    return await resolve_dependencies(graph, context, DependencyCache() if cache is None else cache)


async def _decoded(websocket: WebSocket, encoding: Encoding) -> AsyncIterator[Any]:
    """Every message of the connection, as `encoding` says to read it.

    A message of the wrong kind is a client sending something the endpoint did
    not declare it accepts, which the underlying `receive_text`/`receive_bytes`
    report by raising — so nothing here has to guess what was meant.
    """
    while True:
        try:
            if encoding == "text":
                yield await websocket.receive_text()
            elif encoding == "bytes":
                yield await websocket.receive_bytes()
            else:
                yield await websocket.receive_json()
        except WebSocketDisconnect:
            return


def _report_failure(task: asyncio.Task[None]) -> None:
    """Log what a stream handler failed with.

    A task's exception is otherwise only seen if somebody awaits it, and
    nothing awaits these — without this a handler that raised would disappear
    silently.
    """
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("a WebTransport stream handler failed", exc_info=error)


def _decode_datagram(payload: bytes, encoding: Encoding) -> Any:
    """One datagram, as `encoding` says to read it."""
    if encoding == "bytes":
        return payload
    if encoding == "text":
        return payload.decode("utf-8")
    return json_module.loads(payload)


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

    #: How each message is handed to `on_receive`.
    encoding: Encoding = "text"

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
            async for data in _decoded(websocket, self.encoding):
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

    async def on_disconnect(self, websocket: WebSocket, close_code: int, **params) -> None:
        """Override to handle disconnection."""


class WebTransportEndpoint:
    """
    Base class for WebTransport endpoints with support for datagrams and streams.
    """

    #: Decorators wrapped around this endpoint, outermost first, so the one
    #: named first is the first to run. Applied once when the route table is
    #: built, and around the whole session rather than one method of it — for a
    #: single method, decorate it with `nitro.utils.decorators.method_decorator`.
    decorators: Sequence[Callable[..., Any]] = ()

    #: How each datagram is handed to `on_datagram`. Streams are not
    #: decoded: a stream is read through its own methods, which already
    #: offer text, bytes and JSON.
    encoding: Encoding = "bytes"
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
        #: Streams are handled concurrently, so a slow one does not hold up the
        #: next. The tasks are held here because a task nothing refers to may be
        #: collected while it is still running.
        running: set[asyncio.Task[None]] = set()

        async def handle_datagrams() -> None:
            async for payload in session.iter_datagrams():
                data = _decode_datagram(payload, self.encoding)
                await self.on_datagram(session, data, **datagram, **params)

        async def handle_streams() -> None:
            async for stream in session.iter_streams():
                task = asyncio.create_task(
                    self.on_stream(session, stream, **stream_supplied, **params)
                )
                running.add(task)
                task.add_done_callback(running.discard)
                task.add_done_callback(_report_failure)

        try:
            if self.use_streams:
                # Both loops end when the session does. `gather` without
                # `return_exceptions` re-raises the first failure here, where
                # the `except` below can see it, rather than returning it as a
                # value nothing reads.
                await asyncio.gather(handle_datagrams(), handle_streams())
            else:
                await handle_datagrams()
        except WebTransportDisconnect as exc:
            close_code = exc.code
        finally:
            for task in list(running):
                task.cancel()
            await self.on_disconnect(session, close_code, **disconnect, **params)

    async def on_connect(self, session: WebTransportSession, **params) -> None:
        """Override to handle session connection."""
        await session.accept()

    async def on_datagram(self, session: WebTransportSession, data: Any, **params) -> None:
        """Override to handle incoming datagrams."""

    async def on_stream(self, session: WebTransportSession, stream: Any, **params) -> None:
        """Override to handle incoming streams (only called if use_streams=True)."""

    async def on_disconnect(self, session: WebTransportSession, close_code: int, **params) -> None:
        """Override to handle disconnection."""
