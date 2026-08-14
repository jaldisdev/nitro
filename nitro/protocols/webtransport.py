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

"""WebTransport sessions.

A comfortable surface over the compiled session. Datagrams are unordered and
may be lost; streams are ordered and reliable. Which of the two to use is a
decision only the application can make, so both are offered plainly rather than
hidden behind one interface.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import Enum
from typing import Any

from nitro.protocols.http import URL, Address, QueryParams, State
from nitro.utils import json as json_module

__all__ = [
    "WebTransportDisconnect",
    "WebTransportSession",
    "WebTransportState",
    "WebTransportStream",
]


class WebTransportDisconnect(Exception):
    """The session ended."""

    def __init__(self, code: int = 0, reason: str | None = None):
        self.code = code
        self.reason = reason or ""
        super().__init__(f"WebTransport session closed with code {code}")


class WebTransportState(Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


class WebTransportStream:
    """One stream within a session."""

    def __init__(self, stream: Any):
        self._stream = stream

    @property
    def readable(self) -> bool:
        return self._stream.readable

    @property
    def writable(self) -> bool:
        return self._stream.writable

    async def send(self, data: bytes) -> None:
        await self._stream.write(data)

    async def send_text(self, text: str) -> None:
        await self._stream.write(text.encode("utf-8"))

    async def send_json(self, data: Any) -> None:
        await self._stream.write(json_module.dumps(data))

    async def receive(self, limit: int = 65536) -> bytes:
        return await self._stream.read(limit)

    async def receive_all(self) -> bytes:
        return await self._stream.read_all()

    async def receive_text(self) -> str:
        return (await self.receive_all()).decode("utf-8")

    async def receive_json(self) -> Any:
        return json_module.loads(await self.receive_all())

    async def finish(self) -> None:
        """Say that nothing more will be written."""
        await self._stream.finish()

    def __repr__(self) -> str:
        directions = []
        if self.readable:
            directions.append("readable")
        if self.writable:
            directions.append("writable")
        return f"WebTransportStream({', '.join(directions) or 'closed'})"


class WebTransportSession:
    """One WebTransport session."""

    def __init__(self, scope: Any, session: Any, path_params: dict[str, Any] | None = None):
        self.scope = scope
        self._session = session
        # Taken as given rather than copied: what the application passes is
        # built for this request and handed over, so copying it again is a
        # dictionary per request that nothing reads differently.
        self._path_params = path_params if path_params is not None else dict(scope.path_params)
        self._state = State()
        self._url: URL | None = None
        self._query_params: QueryParams | None = None

    @property
    def session(self) -> Any:
        """The compiled session, for anything this surface does not cover."""
        return self._session

    @property
    def connected(self) -> bool:
        """Whether the session has been accepted and is still open."""
        return self._session.connected

    @property
    def connection_state(self) -> WebTransportState:
        if self._session.connected:
            return WebTransportState.CONNECTED
        return WebTransportState.CONNECTING

    @property
    def url(self) -> URL:
        if self._url is None:
            self._url = URL("https", self.scope.authority, self.scope.path, self.scope.query_string)
        return self._url

    @property
    def path(self) -> str:
        return self.scope.path

    @property
    def headers(self) -> Any:
        return self.scope.headers

    @property
    def query_params(self) -> QueryParams:
        if self._query_params is None:
            self._query_params = QueryParams(self.scope.query_string)
        return self._query_params

    @property
    def path_params(self) -> dict[str, Any]:
        return self._path_params

    @property
    def client(self) -> Address | None:
        pair = self.scope.client
        return Address(*pair) if pair else None

    @property
    def state(self) -> State:
        return self._state

    async def accept(self) -> None:
        await self._session.accept()

    async def reject(self, status: int = 403) -> None:
        await self._session.reject(status)

    async def close(self) -> None:
        await self._session.close()

    # ── datagrams ────────────────────────────────────────────────────────────

    def send_datagram(self, data: bytes) -> None:
        """Send a datagram. Delivery is not guaranteed and order is not kept."""
        self._session.send_datagram(data)

    def send_datagram_text(self, text: str) -> None:
        self._session.send_datagram(text.encode("utf-8"))

    def send_datagram_json(self, data: Any) -> None:
        self._session.send_datagram(json_module.dumps(data))

    async def receive_datagram(self) -> bytes:
        """The next datagram, raising when the session has ended."""
        payload = await self._session.receive_datagram()
        if payload is None:
            raise WebTransportDisconnect()
        return payload

    async def iter_datagrams(self) -> AsyncIterator[bytes]:
        while True:
            payload = await self._session.receive_datagram()
            if payload is None:
                return
            yield payload

    # ── streams ──────────────────────────────────────────────────────────────

    async def open_stream(self) -> WebTransportStream:
        """Open a stream to the client that both sides can use."""
        return WebTransportStream(await self._session.open_stream())

    async def open_outgoing(self) -> WebTransportStream:
        """Open a stream to the client that only this side writes."""
        return WebTransportStream(await self._session.open_outgoing())

    async def accept_stream(self) -> WebTransportStream:
        """The next stream the client opened, raising when the session ends."""
        stream = await self._session.accept_stream()
        if stream is None:
            raise WebTransportDisconnect()
        return WebTransportStream(stream)

    async def iter_streams(self) -> AsyncIterator[WebTransportStream]:
        while True:
            stream = await self._session.accept_stream()
            if stream is None:
                return
            yield WebTransportStream(stream)

    def __repr__(self) -> str:
        return f"WebTransportSession(path={self.path!r}, state={self.connection_state.value})"
