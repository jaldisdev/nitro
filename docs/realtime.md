# WebSocket and WebTransport

Both are served by the same process and the same application object as HTTP,
and both are routed through the same table.

## WebSocket

```python
from nitro.protocols.websocket import WebSocket


@app.websocket("/rooms/<slug:room>")
async def room(socket: WebSocket, room: str) -> None:
    await socket.accept()

    async for message in socket:
        await socket.send_text(f"{room}: {message}")
```

Nothing is sent until the handler accepts. Until then the client has had no
response at all, which is what lets a handler refuse:

```python
@app.websocket("/private")
async def private(socket: WebSocket) -> None:
    if not authorised(socket.headers.get("authorization")):
        await socket.reject(403, "not for you")
        return
    await socket.accept()
```

A refusal is an ordinary HTTP response, so a client sees a status rather than a
connection that opens and immediately closes. A handler that returns without
doing either refuses with a 500 rather than leaving the client waiting.

### Subprotocols

```python
@app.websocket("/chat")
async def chat(socket: WebSocket) -> None:
    offered: tuple[str, ...] = socket.subprotocols
    await socket.accept("v2" if "v2" in offered else None)
```

Only a subprotocol the client offered may be chosen; naming one it did not would
leave the two sides disagreeing about what they are speaking.

### Reading and writing

```python
text: str = await socket.receive_text()
data: bytes = await socket.receive_bytes()
payload = await socket.receive_json()

await socket.send_text("hello")
await socket.send_bytes(b"\x00\x01")
await socket.send_json({"event": "joined"})

await socket.close(1000, "done")
```

`receive()` raises `WebSocketDisconnect` when the connection has ended;
iterating with `async for` simply stops. Ping and pong frames are answered by
the transport and never surface.

**Reading and writing may happen at once.** The two directions are held
independently, so this is a normal thing to write:

```python
@app.websocket("/duplex")
async def duplex(socket: WebSocket) -> None:
    await socket.accept()

    async def heartbeat() -> None:
        while True:
            await socket.send_text("ping")
            await asyncio.sleep(30)

    pump = asyncio.create_task(heartbeat())
    try:
        async for message in socket:
            await socket.send_text(f"echo: {message}")
    finally:
        pump.cancel()
```

## WebTransport

WebTransport runs over HTTP/3, so it needs `HTTP` set to `"auto"` or `"3"` and a
TLS certificate. It is switched off automatically when HTTP/3 is not available.

```python
from nitro.protocols.webtransport import WebTransportSession


@app.webtransport("/live")
async def live(session: WebTransportSession) -> None:
    await session.accept()

    async for payload in session.iter_datagrams():
        session.send_datagram(payload)
```

### Datagrams

Unordered, and may be lost. Right for anything where the newest value matters
more than every value — cursor positions, telemetry, game state.

```python
session.send_datagram(b"...")
session.send_datagram_json({"x": 1, "y": 2})

payload: bytes = await session.receive_datagram()
```

Datagrams arriving while nothing is reading are held in a ring of
`DATAGRAM_QUEUE_CAPACITY`. When it is full the *oldest* is dropped: a receiver
that has fallen behind is better served by recent data than by a backlog, and
unlike a stream a datagram carries no promise of delivery to break.

### Streams

Ordered and reliable. Right for anything that must arrive whole.

```python
stream = await session.open_stream()          # both sides can use it
await stream.send_text("hello")
reply: str = await stream.receive_text()
await stream.finish()

outgoing = await session.open_outgoing()      # this side only writes

async for incoming in session.iter_streams():
    body: bytes = await incoming.receive_all()
```

A unidirectional stream carries only the half its direction allows; using the
other half is an error rather than a silent no-op.

## Combining with Intercom

A socket handler reaching [Intercom](intercom.md) stays in one process — it is
the same publish/subscribe core, in the same interpreter, with no second package
involved.

```python
from nitro.intercom import intercom, new_channel


@app.websocket("/rooms/<slug:room>")
async def room(socket: WebSocket, room: str) -> None:
    await socket.accept()
    channel: str = new_channel("socket")

    async with intercom.listen(channel, groups=[room]) as messages:
        async def outbound() -> None:
            async for message in messages:
                await socket.send_text(message["text"])

        pump = asyncio.create_task(outbound())
        try:
            async for incoming in socket:
                await intercom.group_publish(room, {"text": incoming})
        finally:
            pump.cancel()
```

Joining the group on entry and leaving on exit means a socket that goes away
cannot leave its channel registered in a room, receiving messages nobody reads.
