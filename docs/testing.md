# Testing

`nitro.testing.TestClient` drives an application in-process. It calls the entry
points the compiled server calls, and finds the route with the compiled matcher,
so a request goes through middleware, dependencies and exception handlers the
way it would behind a socket. What it cannot reach is the transport: TLS, the
`ALLOWED_HOSTS` check and the wire format all answer before the application is
involved.

```python
from nitro.testing import TestClient


async def test_show_user():
    async with TestClient(app) as client:
        response = await client.get("/users/7")

    assert response.status_code == 200
    assert response.json() == {"id": 7}
```

The client is asynchronous, like everything it drives. Entering it as a context
manager runs the startup callbacks and opens worker-scoped dependencies, and
leaving closes them again; a client used without `async with` runs neither.

## HTTP

```python
response = await client.post(
    "/articles",
    params={"draft": "1"},
    headers={"authorization": "Bearer token"},
    json={"title": "Hello"},
)

response.status_code  # int
response.headers      # case-insensitive, like request.headers
response.content      # bytes
response.text         # decoded with the charset the response named
response.json()
```

A body is one of `content` (bytes or text, sent as given), `json`, or `data` for
a form. `files` turns the form into `multipart/form-data`:

```python
await client.post(
    "/documents",
    data={"title": "Report"},
    files={"document": ("report.pdf", payload, "application/pdf")},
)
```

Files sent with `FileResponse` are answered by the server's own file code, so
content types, `Last-Modified`, ranges and `416` come out as they would in
production.

### Cookies

Cookies a response sets are kept in `client.cookies` and sent with every later
request, WebSocket and WebTransport connection included. A cookie that expires
is dropped. `cookies=` adds some for one request only.

### Streaming responses

`request()` waits for the handler to finish and returns the whole body. A
response that is meant to stay open — server-sent events, say — is read with
`stream()` instead:

```python
async with client.stream("GET", "/events") as response:
    async for chunk in response.iter_text():
        ...
        break
```

Leaving the block disconnects, which `request.protocol.client_disconnect()`
reports to the handler, and then waits for the handler to return. Nothing is
cancelled on its behalf, as nothing is behind a real server.

## WebSocket

```python
async with client.websocket("/rooms/4", subprotocols=["v2"]) as socket:
    socket.accepted_subprotocol  # "v2", or None

    await socket.send_json({"event": "join"})
    message = await socket.receive_json()
```

A handshake the application refuses raises `WebSocketRejected` with its status
and reason; an unknown path is a `404` and a handler that returns without
answering a `500`, as with the server. Once the application closes the
connection, `receive()` raises `WebSocketDisconnect` with the close code — `1006`
when the handler returned and left the socket open. Leaving the block closes
the client's end and waits for the handler.

## WebTransport

```python
async with client.webtransport("/game") as session:
    session.send_datagram(b"ping")
    reply = await session.receive_datagram()

    stream = await session.open_stream()      # accept_stream() in the handler
    upload = await session.open_outgoing()    # session.session.accept_incoming()
    pushed = await session.accept_stream()    # open_stream() in the handler
    notice = await session.accept_incoming()  # open_outgoing() in the handler
```

Streams are `WebTransportStream`s, the same class a handler receives. A session
the application refuses raises `WebTransportRejected`; once the application
closes it, reading raises `WebTransportDisconnect`. Datagrams are delivered
reliably and in order here, which a real network does not promise, so a test
that depends on loss has to arrange it itself.
