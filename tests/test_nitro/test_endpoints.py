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

"""The class-based endpoints.

`WebSocketEndpoint` and `WebTransportEndpoint` had no tests, and every one of
their dispatch loops called a method that does not exist — `websocket.iter`,
`session.iter_datagrams(encoding)`, `session.receive_stream`. These drive both
classes end to end so that cannot happen again unnoticed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from nitro.endpoints import HTTPEndpoint, WebSocketEndpoint, WebTransportEndpoint
from nitro.protocols.http import HttpRequest, HttpResponse
from nitro.protocols.websocket import WebSocket
from nitro.protocols.webtransport import WebTransportSession

# ── doubles ───────────────────────────────────────────────────────────────────


class FakeScope:
    """Attributes only, as the compiled scope is."""

    __slots__ = (
        "authority",
        "client",
        "headers",
        "http_version",
        "method",
        "path",
        "path_params",
        "proto",
        "query_string",
        "scheme",
        "server",
        "subprotocols",
    )

    def __init__(self, proto="websocket", path="/", method="GET"):
        object.__setattr__(self, "proto", proto)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "query_string", "")
        object.__setattr__(self, "scheme", "http")
        object.__setattr__(self, "authority", "localhost:8000")
        object.__setattr__(self, "http_version", "1.1")
        object.__setattr__(self, "headers", {})
        object.__setattr__(self, "client", ("127.0.0.1", 9000))
        object.__setattr__(self, "server", ("localhost", 8000))
        object.__setattr__(self, "path_params", {})
        object.__setattr__(self, "subprotocols", ())


class FakeSocketTransport:
    """A WebSocket transport that replays a fixed list of messages."""

    def __init__(self, messages=()):
        self._messages = list(messages)
        self.connected = False
        self.sent: list[str | bytes] = []
        self.closed: tuple[int, str] | None = None
        self.rejected: tuple[int, str] | None = None

    async def accept(self, subprotocol=None):
        self.connected = True

    async def reject(self, status, reason):
        self.rejected = (status, reason)

    async def receive(self):
        if not self._messages:
            return None
        return self._messages.pop(0)

    async def send_str(self, data):
        self.sent.append(data)

    async def send_bytes(self, data):
        self.sent.append(data)

    async def close(self, code=1000, reason=""):
        self.connected = False
        self.closed = (code, reason)


class FakeStream:
    def __init__(self, payload=b""):
        self._payload = payload
        self.readable = True
        self.writable = True
        self.written: list[bytes] = []
        self.finished = False

    async def read(self, limit=65536):
        return self._payload

    async def read_all(self):
        return self._payload

    async def write(self, data):
        self.written.append(data)

    async def finish(self):
        self.finished = True


class FakeSession:
    """A WebTransport session that replays fixed datagrams and streams."""

    def __init__(self, datagrams=(), streams=()):
        self._datagrams = list(datagrams)
        self._streams = list(streams)
        self.connected = False
        self.sent: list[bytes] = []
        self.rejected: int | None = None
        self.was_closed = False

    async def accept(self):
        self.connected = True

    async def reject(self, status):
        self.rejected = status

    async def close(self):
        self.connected = False
        self.was_closed = True

    def send_datagram(self, data):
        self.sent.append(data)

    async def receive_datagram(self):
        if not self._datagrams:
            return None
        return self._datagrams.pop(0)

    async def accept_stream(self):
        if not self._streams:
            # A session with no more streams must park rather than report the
            # end, or the stream loop would finish before the datagram one.
            await asyncio.sleep(0.01)
            return None
        return self._streams.pop(0)

    async def open_stream(self):
        return FakeStream()

    async def open_outgoing(self):
        return FakeStream()


def make_socket(messages=()):
    transport = FakeSocketTransport(messages)
    return WebSocket(FakeScope(), transport), transport


def make_session(datagrams=(), streams=()):
    session = FakeSession(datagrams, streams)
    return WebTransportSession(FakeScope(proto="webtransport"), session), session


# ── HTTP ──────────────────────────────────────────────────────────────────────


class TestHTTPEndpoint:
    async def test_it_dispatches_on_the_verb(self):
        class Endpoint(HTTPEndpoint):
            async def get(self, request):
                return HttpResponse(content="got")

            async def post(self, request):
                return HttpResponse(content="posted")

        class Protocol:
            disconnected = False

            async def __call__(self):
                return b""

        got = await Endpoint()(HttpRequest(FakeScope(proto="http"), Protocol()))
        assert got.body == b"got"

        posted = await Endpoint()(HttpRequest(FakeScope(proto="http", method="POST"), Protocol()))
        assert posted.body == b"posted"


# ── WebSocket ─────────────────────────────────────────────────────────────────


class TestWebSocketEndpoint:
    async def test_it_accepts_and_receives_text(self):
        received = []

        class Endpoint(WebSocketEndpoint):
            encoding = "text"

            async def on_receive(self, websocket, data):
                received.append(data)

        socket, transport = make_socket(["one", "two"])
        await Endpoint()(socket)

        assert received == ["one", "two"]
        assert transport.connected is True

    async def test_bytes_encoding_hands_over_bytes(self):
        received = []

        class Endpoint(WebSocketEndpoint):
            encoding = "bytes"

            async def on_receive(self, websocket, data):
                received.append(data)

        socket, _ = make_socket([b"one", b"two"])
        await Endpoint()(socket)

        assert received == [b"one", b"two"]

    async def test_json_encoding_decodes_each_message(self):
        received = []

        class Endpoint(WebSocketEndpoint):
            encoding = "json"

            async def on_receive(self, websocket, data):
                received.append(data)

        socket, _ = make_socket([json.dumps({"a": 1}), json.dumps([2, 3])])
        await Endpoint()(socket)

        assert received == [{"a": 1}, [2, 3]]

    async def test_disconnect_runs_when_the_client_goes_away(self):
        closed = []

        class Endpoint(WebSocketEndpoint):
            async def on_disconnect(self, websocket, close_code):
                closed.append(close_code)

        socket, _ = make_socket(["only"])
        await Endpoint()(socket)

        assert closed == [1000]

    async def test_disconnect_runs_even_when_a_hook_fails(self):
        closed = []

        class Endpoint(WebSocketEndpoint):
            async def on_receive(self, websocket, data):
                raise RuntimeError("deliberate")

            async def on_disconnect(self, websocket, close_code):
                closed.append(close_code)

        socket, _ = make_socket(["boom"])
        with pytest.raises(RuntimeError, match="deliberate"):
            await Endpoint()(socket)

        assert closed == [1000], "on_disconnect must still run"

    async def test_path_parameters_reach_every_hook(self):
        seen = {}

        class Endpoint(WebSocketEndpoint):
            async def on_connect(self, websocket, room):
                seen["connect"] = room
                await websocket.accept()

            async def on_receive(self, websocket, data, room):
                seen["receive"] = room

            async def on_disconnect(self, websocket, close_code, room):
                seen["disconnect"] = room

        socket, _ = make_socket(["hello"])
        await Endpoint()(socket, room="42")

        assert seen == {"connect": "42", "receive": "42", "disconnect": "42"}


# ── WebTransport ──────────────────────────────────────────────────────────────


class TestWebTransportEndpoint:
    async def test_it_accepts_and_receives_datagrams(self):
        received = []

        class Endpoint(WebTransportEndpoint):
            async def on_datagram(self, session, data):
                received.append(data)

        session, raw = make_session(datagrams=[b"one", b"two"])
        await Endpoint()(session)

        assert received == [b"one", b"two"]
        assert raw.connected is True

    async def test_text_encoding_decodes_each_datagram(self):
        received = []

        class Endpoint(WebTransportEndpoint):
            encoding = "text"

            async def on_datagram(self, session, data):
                received.append(data)

        session, _ = make_session(datagrams=["héllo".encode()])
        await Endpoint()(session)

        assert received == ["héllo"]

    async def test_json_encoding_decodes_each_datagram(self):
        received = []

        class Endpoint(WebTransportEndpoint):
            encoding = "json"

            async def on_datagram(self, session, data):
                received.append(data)

        session, _ = make_session(datagrams=[json.dumps({"a": 1}).encode()])
        await Endpoint()(session)

        assert received == [{"a": 1}]

    async def test_streams_are_handled_when_asked_for(self):
        handled = []

        class Endpoint(WebTransportEndpoint):
            use_streams = True

            async def on_stream(self, session, stream):
                handled.append(await stream.receive_all())

        session, _ = make_session(datagrams=[b"d"], streams=[FakeStream(b"payload")])
        await Endpoint()(session)

        # The stream tasks run alongside the datagram loop; give them a tick to
        # finish once the session has ended.
        await asyncio.sleep(0.05)
        assert handled == [b"payload"]

    async def test_a_failing_stream_handler_is_logged_rather_than_lost(self, caplog):
        import logging

        class Endpoint(WebTransportEndpoint):
            use_streams = True

            async def on_stream(self, session, stream):
                raise RuntimeError("deliberate")

        session, _ = make_session(datagrams=[b"d"], streams=[FakeStream(b"x")])
        with caplog.at_level(logging.ERROR, logger="nitro.endpoints"):
            await Endpoint()(session)
            await asyncio.sleep(0.05)

        assert any("stream handler failed" in record.getMessage() for record in caplog.records)

    async def test_disconnect_runs_when_the_session_ends(self):
        closed = []

        class Endpoint(WebTransportEndpoint):
            async def on_disconnect(self, session, close_code):
                closed.append(close_code)

        session, _ = make_session(datagrams=[b"one"])
        await Endpoint()(session)

        assert closed == [0]
