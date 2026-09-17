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

"""Driving an application in-process, for tests.

The client calls the same entry points the compiled server calls, with the route
found by the compiled matcher, so a request takes the way it would take behind a
socket: middleware, dependencies, exception handlers and all. What stays out of
reach is the transport itself — TLS, the `ALLOWED_HOSTS` check, framing — which
answers before the application is involved.

    async with TestClient(app) as client:
        response = await client.get("/users/7")

        async with client.websocket("/rooms/1") as socket:
            await socket.send_text("hello")

        async with client.webtransport("/game") as session:
            session.send_datagram(b"ping")
"""

from __future__ import annotations

import asyncio
import contextlib
import http.cookies as http_cookies
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlsplit

from nitro._nitro import Headers, RouteMatcher, file_response_parts
from nitro.app import serve_http
from nitro.protocols.websocket import WebSocketDisconnect
from nitro.protocols.webtransport import WebTransportDisconnect, WebTransportStream
from nitro.routing.router import WEBSOCKET_METHOD, WEBTRANSPORT_METHOD
from nitro.utils import json as json_module

if TYPE_CHECKING:
    from nitro.app import Nitro

__all__ = [
    "TestClient",
    "TestResponse",
    "TestStreamResponse",
    "TestWebSocket",
    "TestWebTransport",
    "WebSocketRejected",
    "WebTransportRejected",
]

HeaderInput = Mapping[str, str] | Sequence[tuple[str, str]]
FileInput = tuple[str, bytes] | tuple[str, bytes, str]

_PENDING = "pending"
_OPEN = "open"
_CLOSED = "closed"

_INTERNAL_ERROR = (500, [("content-type", "text/plain; charset=utf-8")], b"Internal Server Error")


class WebSocketRejected(Exception):
    """The application refused the WebSocket handshake."""

    def __init__(self, status: int, reason: str):
        self.status = status
        self.reason = reason
        super().__init__(f"WebSocket handshake rejected with {status}: {reason}")


class WebTransportRejected(Exception):
    """The application refused the WebTransport session."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"WebTransport session rejected with {status}")


def _checked_status(status: int) -> int:
    # The range the compiled transports accept as a status code.
    if not 100 <= status <= 999:
        raise ValueError(f"{status} is not an HTTP status code")
    return status


# ── scope ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TestScope:
    """What the compiled scope carries, for all three protocols."""

    proto: str
    method: str
    path: str
    query_string: str
    scheme: str
    http_version: str
    authority: str | None
    headers: Headers
    client: tuple[str, int] | None
    server: tuple[str, int] | None
    route_id: int | None
    path_params: dict[str, str]
    allowed_methods: tuple[str, ...] = ()
    subprotocols: tuple[str, ...] = ()


# ── HTTP ─────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Answer:
    status: int
    headers: list[tuple[str, str]]
    body: bytes = b""
    path: str | None = None
    start: int | None = None
    end: int | None = None
    stream: TestStreamTransport | None = None


class TestStreamTransport:
    """The writing end of a streaming response.

    Unbounded, unlike the compiled one: nothing here is slowed to a client's
    pace, and a test that reads a response only once its handler has finished
    must not stall the handler on a full queue.
    """

    def __init__(self, disconnected: asyncio.Event, capacity: int):
        self._chunks: deque[bytes] = deque()
        self._changed = asyncio.Event()
        self._disconnected = disconnected
        self._capacity = capacity
        self._open = True

    def send_bytes(self, chunk: bytes) -> Awaitable[None]:
        return self._send(bytes(chunk))

    def send_str(self, text: str) -> Awaitable[None]:
        return self._send(text.encode("utf-8"))

    def close(self) -> None:
        self._open = False
        self._changed.set()

    @property
    def closed(self) -> bool:
        return not self._open or self._disconnected.is_set()

    @property
    def capacity(self) -> int:
        self._active()
        return self._capacity

    def _active(self) -> None:
        if not self._open:
            raise RuntimeError("this response stream is closed")

    def _send(self, chunk: bytes) -> Awaitable[None]:
        self._active()

        async def send() -> None:
            if self._disconnected.is_set():
                raise RuntimeError("the client stopped reading the response")
            self._chunks.append(chunk)
            self._changed.set()

        return send()

    async def _next_chunk(self, handler: asyncio.Task[None]) -> bytes | None:
        """The next chunk, or `None` once the response has ended.

        A handler that returns without closing the stream ends the response as
        well, the way dropping the compiled transport does.
        """
        while True:
            if self._chunks:
                return self._chunks.popleft()
            if not self._open or handler.done():
                return None
            self._changed.clear()
            waiter = asyncio.ensure_future(self._changed.wait())
            try:
                await asyncio.wait({waiter, handler}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()


class TestHttpProtocol:
    """Stands where the compiled `HttpProtocol` stands, recording the answer."""

    def __init__(self, body: bytes, stream_capacity: int):
        self._body: deque[bytes] = deque([body] if body else [])
        self._stream_capacity = stream_capacity
        self._answer: _Answer | None = None
        self.answered = asyncio.Event()
        self.disconnect = asyncio.Event()
        self.failure: str | None = None

    @property
    def answer(self) -> _Answer | None:
        return self._answer

    def _handler_ended(self) -> None:
        return None

    def _handler_failed(self, error: str) -> None:
        if self._answer is None:
            self.failure = error

    async def __call__(self) -> bytes:
        rest = b"".join(self._body)
        self._body.clear()
        return rest

    def __aiter__(self) -> TestHttpProtocol:
        return self

    async def __anext__(self) -> bytes:
        if not self._body:
            raise StopAsyncIteration
        return self._body.popleft()

    async def client_disconnect(self) -> None:
        await self.disconnect.wait()

    @property
    def disconnected(self) -> bool:
        return self.disconnect.is_set()

    def response_empty(self, status: int, headers: Sequence[tuple[str, str]] = ()) -> None:
        self._respond(_Answer(status, self._headers(headers)))

    def response_bytes(
        self, status: int, headers: Sequence[tuple[str, str]] = (), body: Any = None
    ) -> None:
        if isinstance(body, str):
            raise TypeError("response_bytes takes bytes; use response_str for text")
        self._respond(_Answer(status, self._headers(headers), b"" if body is None else bytes(body)))

    def response_str(
        self, status: int, headers: Sequence[tuple[str, str]] = (), body: str = ""
    ) -> None:
        self._respond(_Answer(status, self._headers(headers), body.encode("utf-8")))

    def response_file(
        self, status: int, headers: Sequence[tuple[str, str]] = (), path: str = ""
    ) -> None:
        self._respond(_Answer(status, self._headers(headers), path=path))

    def response_file_range(
        self,
        status: int,
        headers: Sequence[tuple[str, str]] = (),
        path: str = "",
        start: int = 0,
        end: int | None = None,
    ) -> None:
        self._respond(_Answer(status, self._headers(headers), path=path, start=start, end=end))

    def response_stream(
        self, status: int, headers: Sequence[tuple[str, str]] = ()
    ) -> TestStreamTransport:
        transport = TestStreamTransport(self.disconnect, self._stream_capacity)
        self._respond(_Answer(status, self._headers(headers), stream=transport))
        return transport

    @staticmethod
    def _headers(headers: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
        pairs = [(name, value) for name, value in headers]
        # Built for its validation only: a name or value the compiled protocol
        # would refuse is refused here too.
        Headers(pairs)
        return pairs

    def _respond(self, answer: _Answer) -> None:
        if self._answer is not None:
            raise RuntimeError("a response has already been sent for this request")
        _checked_status(answer.status)
        self._answer = answer
        self.answered.set()


@dataclass(slots=True)
class TestResponse:
    """A complete response."""

    status_code: int
    headers: Headers
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode(_charset(self.headers))

    def json(self) -> Any:
        return json_module.loads(self.content)

    @property
    def cookies(self) -> dict[str, str]:
        """The cookies this response set, by name."""
        return {
            name: morsel.value
            for header in self.headers.get_all("set-cookie")
            for name, morsel in http_cookies.SimpleCookie(header).items()
        }


class TestStreamResponse:
    """A response whose body is read as it is produced."""

    def __init__(self, exchange: _HttpExchange, status_code: int, headers: Headers):
        self._exchange = exchange
        self.status_code = status_code
        self.headers = headers

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        async for chunk in self._exchange.chunks():
            yield chunk

    async def iter_text(self) -> AsyncIterator[str]:
        charset = _charset(self.headers)
        async for chunk in self._exchange.chunks():
            yield chunk.decode(charset)

    async def read(self) -> bytes:
        return b"".join([chunk async for chunk in self._exchange.chunks()])

    def disconnect(self) -> None:
        """Stop reading, as a client that goes away does."""
        self._exchange.protocol.disconnect.set()


def _charset(headers: Headers) -> str:
    content_type = headers.get("content-type", "") or ""
    for parameter in content_type.split(";")[1:]:
        name, _, value = parameter.strip().partition("=")
        if name.lower() == "charset" and value:
            return value.strip('"')
    return "utf-8"


class _HttpExchange:
    """One request, from starting its handler to the end of its response."""

    def __init__(self, application: Nitro, scope: TestScope, protocol: TestHttpProtocol):
        self.scope = scope
        self.protocol = protocol
        self.handler = asyncio.create_task(serve_http(application, scope, protocol))
        self._body: bytes | None = None

    async def start(self) -> tuple[int, Headers]:
        """Wait for the status and headers."""
        waiter = asyncio.ensure_future(self.protocol.answered.wait())
        try:
            await asyncio.wait({waiter, self.handler}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()

        answer = self.protocol.answer
        if answer is None:
            await self.handler
            status, headers, self._body = _INTERNAL_ERROR
            return status, Headers(headers)
        if answer.path is not None:
            status, headers, self._body = await file_response_parts(
                answer.status, answer.headers, answer.path, answer.start, answer.end
            )
            return status, Headers(headers)
        if answer.stream is None:
            self._body = answer.body
        return answer.status, Headers(answer.headers)

    async def chunks(self) -> AsyncIterator[bytes]:
        head = self.scope.method == "HEAD"
        answer = self.protocol.answer
        if answer is not None and answer.stream is not None:
            while (chunk := await answer.stream._next_chunk(self.handler)) is not None:
                if not head:
                    yield chunk
        elif self._body and not head:
            yield self._body
            self._body = b""

    async def finish(self) -> None:
        """Wait for the handler, which may still be running after its answer."""
        await self.handler


# ── WebSocket ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Close:
    code: int
    reason: str


class TestWsTransport:
    """Stands where the compiled `WsTransport` stands."""

    def __init__(self, subprotocols: Sequence[str]):
        self._subprotocols = list(subprotocols)
        self._phase = _PENDING
        self.to_application: asyncio.Queue[str | bytes | _Close] = asyncio.Queue()
        self.to_client: asyncio.Queue[str | bytes | _Close] = asyncio.Queue()
        self.answer: asyncio.Future[tuple[int, str | None]] = (
            asyncio.get_running_loop().create_future()
        )
        self.client_closed = False

    @property
    def subprotocols(self) -> list[str]:
        return list(self._subprotocols)

    async def accept(self, subprotocol: str | None = None) -> None:
        self._pending()
        if subprotocol is not None and subprotocol not in self._subprotocols:
            raise RuntimeError(f'the subprotocol "{subprotocol}" was not offered by the client')
        self._phase = _OPEN
        self.answer.set_result((101, subprotocol))

    def reject(self, status: int = 403, reason: str = "") -> Awaitable[None]:
        _checked_status(status)

        async def reject() -> None:
            self._pending()
            self._phase = _CLOSED
            self.answer.set_result((status, reason))

        return reject()

    async def receive(self) -> str | bytes | None:
        if self._phase != _OPEN:
            return None
        message = await self.to_application.get()
        if isinstance(message, _Close):
            self._phase = _CLOSED
            return None
        return message

    def __aiter__(self) -> TestWsTransport:
        return self

    async def __anext__(self) -> str | bytes:
        message = await self.receive()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send_str(self, text: str) -> None:
        self._send(text)

    async def send_bytes(self, data: bytes) -> None:
        self._send(bytes(data))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._phase == _OPEN and not self.client_closed:
            self.to_client.put_nowait(_Close(code, reason))
        self._phase = _CLOSED

    @property
    def connected(self) -> bool:
        return self._phase == _OPEN

    def ended(self) -> None:
        """The handler returned, which drops the connection it left open."""
        if self._phase == _OPEN and not self.client_closed:
            self.to_client.put_nowait(_Close(1006, ""))
        self._phase = _CLOSED

    def _pending(self) -> None:
        if self._phase != _PENDING:
            raise RuntimeError("this handshake has already been answered")

    def _send(self, message: str | bytes) -> None:
        if self._phase != _OPEN:
            raise RuntimeError("this WebSocket is not open; accept the handshake first")
        if self.client_closed:
            raise RuntimeError("the connection is closed")
        self.to_client.put_nowait(message)


class TestWebSocket:
    """The client's end of an accepted WebSocket."""

    def __init__(self, transport: TestWsTransport, subprotocol: str | None):
        self._transport = transport
        self.accepted_subprotocol = subprotocol
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def send_text(self, text: str) -> None:
        self._sendable()
        self._transport.to_application.put_nowait(text)

    async def send_bytes(self, data: bytes) -> None:
        self._sendable()
        self._transport.to_application.put_nowait(bytes(data))

    async def send_json(self, data: Any) -> None:
        await self.send_text(json_module.dumps_str(data))

    async def receive(self) -> str | bytes:
        """The next message, raising `WebSocketDisconnect` once the application
        has closed the connection."""
        if self.close_code is not None:
            raise WebSocketDisconnect(self.close_code, self.close_reason)
        message = await self._transport.to_client.get()
        if isinstance(message, _Close):
            self.close_code, self.close_reason = message.code, message.reason
            raise WebSocketDisconnect(message.code, message.reason)
        return message

    async def receive_text(self) -> str:
        message = await self.receive()
        if not isinstance(message, str):
            raise TypeError("expected a text message, received binary")
        return message

    async def receive_bytes(self) -> bytes:
        message = await self.receive()
        if not isinstance(message, bytes):
            raise TypeError("expected a binary message, received text")
        return message

    async def receive_json(self) -> Any:
        return json_module.loads(await self.receive())

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._transport.client_closed:
            return
        self._transport.client_closed = True
        self._transport.to_application.put_nowait(_Close(code, reason))

    def _sendable(self) -> None:
        if self._transport.client_closed or self.close_code is not None:
            raise RuntimeError("the connection is closed")


# ── WebTransport ─────────────────────────────────────────────────────────────


class _Pipe:
    """One direction of a WebTransport stream."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._finished = False
        self._changed = asyncio.Condition()

    async def write(self, data: bytes) -> None:
        if self._finished:
            raise RuntimeError("this stream has been finished")
        async with self._changed:
            self._buffer.extend(data)
            self._changed.notify_all()

    async def finish(self) -> None:
        async with self._changed:
            self._finished = True
            self._changed.notify_all()

    async def read(self, limit: int) -> bytes:
        async with self._changed:
            await self._changed.wait_for(lambda: bool(self._buffer) or self._finished)
            taken = bytes(self._buffer[: max(limit, 1)])
            del self._buffer[: len(taken)]
            return taken

    async def read_all(self) -> bytes:
        async with self._changed:
            await self._changed.wait_for(lambda: self._finished)
            taken = bytes(self._buffer)
            self._buffer.clear()
            return taken


class TestWtStream:
    """Stands where the compiled `WtStream` stands, on either side."""

    def __init__(self, send: _Pipe | None, receive: _Pipe | None):
        self._send = send
        self._receive = receive

    async def write(self, data: bytes) -> None:
        if self._send is None:
            raise RuntimeError("this stream cannot be written to")
        await self._send.write(bytes(data))

    async def read(self, limit: int = 65536) -> bytes:
        if self._receive is None:
            raise RuntimeError("this stream cannot be read from")
        return await self._receive.read(limit)

    async def read_all(self) -> bytes:
        if self._receive is None:
            raise RuntimeError("this stream cannot be read from")
        return await self._receive.read_all()

    async def finish(self) -> None:
        if self._send is not None:
            await self._send.finish()

    @property
    def writable(self) -> bool:
        return self._send is not None

    @property
    def readable(self) -> bool:
        return self._receive is not None


def _duplex() -> tuple[TestWtStream, TestWtStream]:
    """Both ends of a bidirectional stream."""
    outgoing, incoming = _Pipe(), _Pipe()
    return TestWtStream(outgoing, incoming), TestWtStream(incoming, outgoing)


def _one_way() -> tuple[TestWtStream, TestWtStream]:
    """The writing and reading ends of a unidirectional stream."""
    pipe = _Pipe()
    return TestWtStream(pipe, None), TestWtStream(None, pipe)


@dataclass(slots=True)
class _Side:
    """What one side of a session has waiting for it."""

    datagrams: asyncio.Queue[bytes | None] = field(default_factory=asyncio.Queue)
    streams: asyncio.Queue[TestWtStream | None] = field(default_factory=asyncio.Queue)
    incoming: asyncio.Queue[TestWtStream | None] = field(default_factory=asyncio.Queue)

    def end(self) -> None:
        self.datagrams.put_nowait(None)
        self.streams.put_nowait(None)
        self.incoming.put_nowait(None)


class TestWtSession:
    """Stands where the compiled `WtSession` stands."""

    def __init__(self) -> None:
        self._phase = _PENDING
        self.application = _Side()
        self.client = _Side()
        self.answer: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self.client_closed = False

    async def accept(self) -> None:
        self._pending()
        self._phase = _OPEN
        self.answer.set_result(200)

    def reject(self, status: int = 403) -> Awaitable[None]:
        _checked_status(status)

        async def reject() -> None:
            self._pending()
            self._phase = _CLOSED
            self.answer.set_result(status)

        return reject()

    @property
    def connected(self) -> bool:
        return self._phase == _OPEN

    def send_datagram(self, payload: bytes) -> None:
        self._opened(sending=True)
        self.client.datagrams.put_nowait(bytes(payload))

    def receive_datagram(self) -> Coroutine[Any, Any, bytes | None]:
        self._opened()
        return self.application.datagrams.get()

    def accept_stream(self) -> Coroutine[Any, Any, TestWtStream | None]:
        self._opened()
        return self.application.streams.get()

    def accept_incoming(self) -> Coroutine[Any, Any, TestWtStream | None]:
        self._opened()
        return self.application.incoming.get()

    def open_stream(self) -> Awaitable[TestWtStream]:
        self._opened(sending=True)

        async def open_stream() -> TestWtStream:
            local, remote = _duplex()
            self.client.streams.put_nowait(remote)
            return local

        return open_stream()

    def open_outgoing(self) -> Awaitable[TestWtStream]:
        self._opened(sending=True)

        async def open_outgoing() -> TestWtStream:
            local, remote = _one_way()
            self.client.incoming.put_nowait(remote)
            return local

        return open_outgoing()

    async def close(self) -> None:
        if self._phase == _OPEN and not self.client_closed:
            self.client.end()
        self._phase = _CLOSED

    def ended(self) -> None:
        """The handler returned, which ends the session it left open."""
        if self._phase == _OPEN and not self.client_closed:
            self.client.end()
        self._phase = _CLOSED

    def _pending(self) -> None:
        if self._phase != _PENDING:
            raise RuntimeError("this session has already been answered")

    def _opened(self, sending: bool = False) -> None:
        if self._phase != _OPEN:
            raise RuntimeError("this WebTransport session is not open")
        # What is still queued for the application stays readable, ending in
        # `None`, once the client has gone.
        if sending and self.client_closed:
            raise RuntimeError("the session is not open")


class TestWebTransport:
    """The client's end of an accepted WebTransport session."""

    def __init__(self, session: TestWtSession):
        self._session = session
        self._ended = False

    def send_datagram(self, data: bytes) -> None:
        self._usable()
        self._session.application.datagrams.put_nowait(bytes(data))

    async def receive_datagram(self) -> bytes:
        """The next datagram, raising `WebTransportDisconnect` once the
        application has ended the session."""
        return self._received(await self._session.client.datagrams.get())

    async def open_stream(self) -> WebTransportStream:
        """Open a stream the application receives from `accept_stream`."""
        self._usable()
        local, remote = _duplex()
        self._session.application.streams.put_nowait(remote)
        return WebTransportStream(local)

    async def open_outgoing(self) -> WebTransportStream:
        """Open a stream the application receives from `accept_incoming`."""
        self._usable()
        local, remote = _one_way()
        self._session.application.incoming.put_nowait(remote)
        return WebTransportStream(local)

    async def accept_stream(self) -> WebTransportStream:
        """The next stream the application opened with `open_stream`."""
        return WebTransportStream(self._received(await self._session.client.streams.get()))

    async def accept_incoming(self) -> WebTransportStream:
        """The next stream the application opened with `open_outgoing`."""
        return WebTransportStream(self._received(await self._session.client.incoming.get()))

    async def close(self) -> None:
        if self._session.client_closed:
            return
        self._session.client_closed = True
        self._session.application.end()

    def _received[Value](self, value: Value | None) -> Value:
        if value is None:
            self._ended = True
            # Put back for whoever else is waiting on the same session.
            self._session.client.end()
            raise WebTransportDisconnect()
        return value

    def _usable(self) -> None:
        if self._session.client_closed or self._ended:
            raise RuntimeError("the session is not open")


# ── client ───────────────────────────────────────────────────────────────────


class TestClient:
    """Sends requests to `application` without a server in between.

    Used as an async context manager, the application's startup callbacks and
    worker-scoped dependencies run on entry and are closed on exit; used without
    one, they do not run at all. Cookies the application sets are kept and sent
    back, as a browser would, in `cookies`.
    """

    __test__ = False

    def __init__(
        self,
        application: Nitro,
        *,
        base_url: str = "http://testserver",
        headers: HeaderInput | None = None,
        cookies: Mapping[str, str] | None = None,
        client: tuple[str, int] | None = ("127.0.0.1", 50000),
        http_version: str = "1.1",
        stream_capacity: int = 16,
    ):
        self.application = application
        parts = urlsplit(base_url)
        self.scheme = parts.scheme or "http"
        self.host = parts.netloc or "testserver"
        self.headers = _pairs(headers)
        self.cookies: dict[str, str] = dict(cookies or {})
        self.client = client
        self.http_version = http_version
        self._stream_capacity = stream_capacity
        self._matcher = RouteMatcher(application.route_table())

    async def __aenter__(self) -> TestClient:
        await self.application._start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.application._stop()

    # ── HTTP ─────────────────────────────────────────────────────────────────

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: HeaderInput | None = None,
        cookies: Mapping[str, str] | None = None,
        content: bytes | str | None = None,
        json: Any = None,
        data: Mapping[str, Any] | None = None,
        files: Mapping[str, FileInput] | None = None,
    ) -> TestResponse:
        exchange = self._exchange(method, url, params, headers, cookies, content, json, data, files)
        status, response_headers = await exchange.start()
        body = b"".join([chunk async for chunk in exchange.chunks()])
        await exchange.finish()
        self._keep_cookies(response_headers)
        return TestResponse(status, response_headers, body)

    @contextlib.asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: HeaderInput | None = None,
        cookies: Mapping[str, str] | None = None,
        content: bytes | str | None = None,
        json: Any = None,
        data: Mapping[str, Any] | None = None,
        files: Mapping[str, FileInput] | None = None,
    ) -> AsyncIterator[TestStreamResponse]:
        """A response to read while it is still being produced.

        Leaving the block disconnects, as a client that stops reading does, and
        then waits for the handler to return. Nothing is cancelled for it: a
        handler that streams indefinitely has to notice the disconnect.
        """
        exchange = self._exchange(method, url, params, headers, cookies, content, json, data, files)
        try:
            status, response_headers = await exchange.start()
            self._keep_cookies(response_headers)
            yield TestStreamResponse(exchange, status, response_headers)
        finally:
            exchange.protocol.disconnect.set()
            await exchange.finish()

    async def get(self, url: str, **options: Any) -> TestResponse:
        return await self.request("GET", url, **options)

    async def head(self, url: str, **options: Any) -> TestResponse:
        return await self.request("HEAD", url, **options)

    async def options(self, url: str, **options: Any) -> TestResponse:
        return await self.request("OPTIONS", url, **options)

    async def post(self, url: str, **options: Any) -> TestResponse:
        return await self.request("POST", url, **options)

    async def put(self, url: str, **options: Any) -> TestResponse:
        return await self.request("PUT", url, **options)

    async def patch(self, url: str, **options: Any) -> TestResponse:
        return await self.request("PATCH", url, **options)

    async def delete(self, url: str, **options: Any) -> TestResponse:
        return await self.request("DELETE", url, **options)

    # ── WebSocket ────────────────────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def websocket(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: HeaderInput | None = None,
        cookies: Mapping[str, str] | None = None,
        subprotocols: Sequence[str] = (),
    ) -> AsyncIterator[TestWebSocket]:
        """An accepted WebSocket, raising `WebSocketRejected` when the
        application refuses the handshake.

        Leaving the block closes the connection if it is still open, then waits
        for the handler to return.
        """
        extra = [("sec-websocket-protocol", ", ".join(subprotocols))] if subprotocols else []
        scope = self._scope(
            WEBSOCKET_METHOD,
            url,
            params,
            [*_pairs(headers), *extra],
            cookies,
            "websocket",
            tuple(subprotocols),
        )
        transport = TestWsTransport(subprotocols)
        handler = asyncio.create_task(self.application.__handle_ws__(scope, transport))
        handler.add_done_callback(lambda _: transport.ended())
        try:
            status, detail = await _handshake(transport.answer, handler, (500, None))
            if status != 101:
                reason = (
                    detail if detail is not None else "the handler did not answer the handshake"
                )
                raise WebSocketRejected(status, reason)
            socket = TestWebSocket(transport, detail)
            try:
                yield socket
            finally:
                await socket.close()
        finally:
            await handler

    # ── WebTransport ─────────────────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def webtransport(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: HeaderInput | None = None,
        cookies: Mapping[str, str] | None = None,
    ) -> AsyncIterator[TestWebTransport]:
        """An accepted WebTransport session, raising `WebTransportRejected` when
        the application refuses it.

        Leaving the block closes the session if it is still open, then waits for
        the handler to return.
        """
        scope = self._scope(
            WEBTRANSPORT_METHOD, url, params, _pairs(headers), cookies, "webtransport"
        )
        session = TestWtSession()
        handler = asyncio.create_task(self.application.__handle_wt__(scope, session))
        handler.add_done_callback(lambda _: session.ended())
        try:
            status = await _handshake(session.answer, handler, 500)
            if status != 200:
                raise WebTransportRejected(status)
            connection = TestWebTransport(session)
            try:
                yield connection
            finally:
                await connection.close()
        finally:
            await handler

    # ── internals ────────────────────────────────────────────────────────────

    def _exchange(
        self,
        method: str,
        url: str,
        params: Mapping[str, Any] | None,
        headers: HeaderInput | None,
        cookies: Mapping[str, str] | None,
        content: bytes | str | None,
        json: Any,
        data: Mapping[str, Any] | None,
        files: Mapping[str, FileInput] | None,
    ) -> _HttpExchange:
        body, content_type = _encode_body(content, json, data, files)
        extra = _pairs(headers)
        if content_type is not None and not any(
            name.lower() == "content-type" for name, _ in extra
        ):
            extra.append(("content-type", content_type))
        if body and not any(name.lower() == "content-length" for name, _ in extra):
            extra.append(("content-length", str(len(body))))
        scope = self._scope(method.upper(), url, params, extra, cookies, "http")
        return _HttpExchange(self.application, scope, TestHttpProtocol(body, self._stream_capacity))

    def _scope(
        self,
        method: str,
        url: str,
        params: Mapping[str, Any] | None,
        headers: list[tuple[str, str]],
        cookies: Mapping[str, str] | None,
        proto: str,
        subprotocols: tuple[str, ...] = (),
    ) -> TestScope:
        parts = urlsplit(url)
        path = parts.path or "/"
        query = parts.query
        if params:
            encoded = urlencode(params, doseq=True)
            query = f"{query}&{encoded}" if query else encoded

        pairs = [*headers]
        names = {name.lower() for name, _ in pairs}
        pairs.extend(pair for pair in self.headers if pair[0].lower() not in names)
        authority = parts.netloc or self.host
        if "host" not in names:
            pairs.insert(0, ("host", authority))
        jar = {**self.cookies, **(cookies or {})}
        if jar and "cookie" not in names:
            pairs.append(("cookie", "; ".join(f"{name}={value}" for name, value in jar.items())))

        route_id, path_params, allowed = self._matcher.find(method, path)
        secure = (parts.scheme or self.scheme) in {"https", "wss"}
        scheme = {"http": "https" if secure else "http", "websocket": "wss" if secure else "ws"}
        return TestScope(
            proto=proto,
            method=method,
            path=path,
            query_string=query,
            scheme=scheme.get(proto, "https"),
            http_version=self.http_version if proto != "webtransport" else "3",
            authority=authority,
            headers=Headers(pairs),
            client=self.client,
            server=None,
            route_id=route_id,
            path_params=path_params,
            allowed_methods=allowed if proto == "http" else (),
            subprotocols=subprotocols,
        )

    def _keep_cookies(self, headers: Headers) -> None:
        now = datetime.now(UTC)
        for header in headers.get_all("set-cookie"):
            for name, morsel in http_cookies.SimpleCookie(header).items():
                if _expired(morsel, now):
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value


def _expired(morsel: http_cookies.Morsel[str], now: datetime) -> bool:
    max_age = morsel["max-age"]
    if max_age:
        return int(max_age) <= 0
    expires = morsel["expires"]
    if expires:
        return parsedate_to_datetime(expires) <= now
    return False


async def _handshake[Answer](
    answer: asyncio.Future[Answer], handler: asyncio.Task[None], unanswered: Answer
) -> Answer:
    """How the application answered, or `unanswered` when its handler returned
    without saying."""
    await asyncio.wait({answer, handler}, return_when=asyncio.FIRST_COMPLETED)
    if answer.done():
        return answer.result()
    # Raises what the handler raised past the application's own handling.
    handler.result()
    return unanswered


def _pairs(headers: HeaderInput | None) -> list[tuple[str, str]]:
    if headers is None:
        return []
    if isinstance(headers, Mapping):
        return list(headers.items())
    return [(name, value) for name, value in headers]


def _encode_body(
    content: bytes | str | None,
    json: Any,
    data: Mapping[str, Any] | None,
    files: Mapping[str, FileInput] | None,
) -> tuple[bytes, str | None]:
    given = [
        name
        for name, value in (("content", content), ("json", json), ("data or files", data or files))
        if value is not None
    ]
    if len(given) > 1:
        raise ValueError(f"a request body is one of content, json or data; got {', '.join(given)}")

    if content is not None:
        return (content.encode("utf-8") if isinstance(content, str) else bytes(content)), None
    if json is not None:
        return json_module.dumps(json), "application/json"
    if files:
        return _multipart(data or {}, files)
    if data is not None:
        return urlencode(data, doseq=True).encode("utf-8"), "application/x-www-form-urlencoded"
    return b"", None


def _multipart(data: Mapping[str, Any], files: Mapping[str, FileInput]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in data.items():
        for item in value if isinstance(value, list | tuple) else [value]:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                + str(item).encode("utf-8")
                + b"\r\n"
            )
    for name, upload in files.items():
        filename, payload = upload[0], upload[1]
        content_type = upload[2] if len(upload) > 2 else "application/octet-stream"
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
            + payload
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
