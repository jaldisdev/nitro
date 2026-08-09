"""A minimal WebTransport client, for driving the server in tests.

There is no ready-made WebTransport client for Python, so this drives an HTTP/3
connection directly: an extended `CONNECT`, then datagrams and streams on the
session it establishes. It is deliberately small — enough to prove the server
speaks the protocol, and no more.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from aioquic.asyncio.client import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import (
    DatagramReceived,
    HeadersReceived,
    WebTransportStreamDataReceived,
)
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import QuicEvent, StreamDataReceived


class SessionRefused(Exception):
    """The server answered the CONNECT with something other than 200."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"the session was refused with {status}")


class WebTransportClient(QuicConnectionProtocol):
    """One WebTransport session, seen from the client side."""

    def __init__(self, *arguments: Any, **options: Any) -> None:
        super().__init__(*arguments, **options)
        self._http = H3Connection(self._quic, enable_webtransport=True)
        self._session_id: int | None = None
        self._established: asyncio.Future[int] = asyncio.get_event_loop().create_future()
        self._datagrams: asyncio.Queue[bytes] = asyncio.Queue()
        # Stream payloads accumulate until the writer says it is finished, so a
        # reader sees whole messages rather than whatever happened to arrive.
        self._stream_data: dict[int, bytearray] = {}
        self._finished_streams: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
        # Streams this side opened. Their inbound data is not reported as an
        # HTTP/3 event, so it is read off the QUIC stream directly.
        self._own_streams: set[int] = set()

    # ── connecting ───────────────────────────────────────────────────────────

    async def establish(self, authority: str, path: str, timeout: float = 10.0) -> int:
        """Send the CONNECT and wait for the server to accept it."""
        self._session_id = self._quic.get_next_available_stream_id(is_unidirectional=False)
        self._http.send_headers(
            stream_id=self._session_id,
            headers=[
                (b":method", b"CONNECT"),
                (b":protocol", b"webtransport"),
                (b":scheme", b"https"),
                (b":authority", authority.encode()),
                (b":path", path.encode()),
            ],
            end_stream=False,
        )
        self.transmit()
        return await asyncio.wait_for(self._established, timeout)

    @property
    def session_id(self) -> int:
        if self._session_id is None:
            raise RuntimeError("the session has not been established")
        return self._session_id

    # ── datagrams ────────────────────────────────────────────────────────────

    def send_datagram(self, payload: bytes) -> None:
        self._http.send_datagram(self.session_id, payload)
        self.transmit()

    async def receive_datagram(self, timeout: float = 10.0) -> bytes:
        return await asyncio.wait_for(self._datagrams.get(), timeout)

    # ── streams ──────────────────────────────────────────────────────────────

    def open_stream(self, unidirectional: bool = False) -> int:
        stream_id = self._http.create_webtransport_stream(
            self.session_id, is_unidirectional=unidirectional
        )
        self._own_streams.add(stream_id)
        self.transmit()
        return stream_id

    def write(self, stream_id: int, payload: bytes, end: bool = True) -> None:
        self._quic.send_stream_data(stream_id, payload, end_stream=end)
        self.transmit()

    async def receive_stream(self, timeout: float = 10.0) -> bytes:
        """The payload of the next stream that finished."""
        _stream_id, payload = await asyncio.wait_for(self._finished_streams.get(), timeout)
        return payload

    # ── event handling ───────────────────────────────────────────────────────

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, StreamDataReceived) and event.stream_id in self._own_streams:
            self._collect(event.stream_id, event.data, event.end_stream)
            return

        for http_event in self._http.handle_event(event):
            self._handle(http_event)

    def _collect(self, stream_id: int, data: bytes, ended: bool) -> None:
        buffer = self._stream_data.setdefault(stream_id, bytearray())
        buffer.extend(data)
        if ended:
            self._finished_streams.put_nowait(
                (stream_id, bytes(self._stream_data.pop(stream_id)))
            )

    def _handle(self, event: Any) -> None:
        if isinstance(event, HeadersReceived) and not self._established.done():
            status = self._status_of(event)
            if status == 200:
                self._established.set_result(self.session_id)
            else:
                self._established.set_exception(SessionRefused(status))

        elif isinstance(event, DatagramReceived):
            self._datagrams.put_nowait(event.data)

        elif isinstance(event, WebTransportStreamDataReceived):
            self._collect(event.stream_id, event.data, event.stream_ended)

    @staticmethod
    def _status_of(event: HeadersReceived) -> int:
        for name, value in event.headers:
            if name == b":status":
                return int(value)
        return 0


@asynccontextmanager
async def webtransport(
    host: str, port: int, path: str = "/", timeout: float = 10.0
) -> AsyncIterator[WebTransportClient]:
    """Open a WebTransport session, or raise :class:`SessionRefused`."""
    configuration = QuicConfiguration(
        is_client=True,
        alpn_protocols=H3_ALPN,
        # The server under test uses a self-signed certificate.
        verify_mode=False,
        max_datagram_frame_size=65536,
    )

    async with connect(
        host,
        port,
        configuration=configuration,
        create_protocol=WebTransportClient,
        wait_connected=True,
    ) as client:
        await client.establish(f"{host}:{port}", path, timeout)
        yield client
