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

"""WebSocket connections.

A thin, comfortable surface over the compiled transport: the same connection
seen through an object that knows about text, bytes and JSON, and that reports
the end of a connection by raising rather than by returning `None` in the
middle of a loop.
"""

from __future__ import annotations

import json as json_module
from collections.abc import AsyncIterator
from enum import Enum
from typing import Any

from nitro.protocols.http import URL, Address, QueryParams, State

__all__ = ["WebSocket", "WebSocketDisconnect", "WebSocketState"]


class WebSocketDisconnect(Exception):
    """The connection ended."""

    def __init__(self, code: int = 1000, reason: str | None = None):
        self.code = code
        self.reason = reason or ""
        super().__init__(f"WebSocket closed with code {code}")


class WebSocketState(Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


class WebSocket:
    """One WebSocket connection."""

    def __init__(self, scope: Any, transport: Any, path_params: dict[str, Any] | None = None):
        self.scope = scope
        self._transport = transport
        self._path_params = dict(path_params) if path_params else dict(scope.path_params)
        self._state = State()
        self._url: URL | None = None
        self._query_params: QueryParams | None = None

    @property
    def transport(self) -> Any:
        """The compiled transport, for anything this surface does not cover."""
        return self._transport

    @property
    def scheme(self) -> str:
        return self.scope.scheme

    @property
    def connected(self) -> bool:
        """Whether the handshake has been accepted and the socket is open."""
        return self._transport.connected

    @property
    def connection_state(self) -> WebSocketState:
        if self._transport.connected:
            return WebSocketState.CONNECTED
        return WebSocketState.CONNECTING

    @property
    def url(self) -> URL:
        if self._url is None:
            self._url = URL(
                self.scope.scheme, self.scope.authority, self.scope.path, self.scope.query_string
            )
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
    def subprotocols(self) -> tuple[str, ...]:
        return tuple(self.scope.subprotocols)

    @property
    def client(self) -> Address | None:
        pair = self.scope.client
        return Address(*pair) if pair else None

    @property
    def state(self) -> State:
        return self._state

    async def accept(self, subprotocol: str | None = None) -> None:
        """Complete the handshake. Only a subprotocol the client offered may be
        chosen."""
        await self._transport.accept(subprotocol)

    async def reject(self, status: int = 403, reason: str = "") -> None:
        """Refuse the upgrade and answer with an ordinary HTTP response."""
        await self._transport.reject(status, reason)

    async def receive(self) -> str | bytes:
        """The next message, raising when the connection has ended."""
        message = await self._transport.receive()
        if message is None:
            raise WebSocketDisconnect()
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
        message = await self.receive()
        return json_module.loads(message)

    async def send_text(self, data: str) -> None:
        await self._transport.send_str(data)

    async def send_bytes(self, data: bytes) -> None:
        await self._transport.send_bytes(data)

    async def send_json(self, data: Any) -> None:
        await self._transport.send_str(json_module.dumps(data))

    async def send(self, data: str | bytes) -> None:
        if isinstance(data, str):
            await self.send_text(data)
        else:
            await self.send_bytes(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self._transport.close(code, reason)

    async def iter_text(self) -> AsyncIterator[str]:
        async for message in self:
            if isinstance(message, str):
                yield message

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        async for message in self:
            if isinstance(message, bytes):
                yield message

    async def iter_json(self) -> AsyncIterator[Any]:
        async for message in self:
            yield json_module.loads(message)

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self._messages()

    async def _messages(self) -> AsyncIterator[str | bytes]:
        while True:
            message = await self._transport.receive()
            if message is None:
                return
            yield message

    def __repr__(self) -> str:
        return f"WebSocket(path={self.path!r}, state={self.connection_state.value})"
