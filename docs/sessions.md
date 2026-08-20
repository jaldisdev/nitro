# Sessions

A session is server-side state held under an opaque key: a bag of
JSON-compatible values kept in a cache, with the key travelling between the
client and the server somehow. Nitro provides the bag and leaves the *somehow*
to you.

```python
MIDDLEWARE = [
    "nitro.middleware.common.OriginMiddleware",
    "nitro.sessions.SessionMiddleware",
]
```

```python
async def preferences(request: HttpRequest) -> HttpResponse:
    session = request.state.session

    await session.set("region", "eu")
    region = await session.get("region", "us")

    return JSONResponse({"region": region})
```

Every operation is a coroutine. A mapping that reads and writes a remote store
behind `session["x"]` hides the one thing worth seeing in an async application,
and the sync interface such a mapping needs is exactly what pushes a store into
blocking calls. The bag is read once per connection and held, so ten `get`
calls cost one round trip — the `await`s are honest about where the trip is,
not one per key.

## What it is not

There is no login, no identity and no user model. A session is state; which
state means "signed in" is the application's to decide. Nitro also does not
provide a device registry — if you want a table of "where am I signed in", with
IP addresses and last-seen times, that is your table and your queries, not this.

The one concession is [`cycle()`](#signing-in), because session fixation has to
be defended against at sign-in and only you know when that is.

## Settings

| Setting | Default | |
|---|---|---|
| `SESSION_CACHE` | `"default"` | Which cache holds the bags. |
| `SESSION_KEY_PREFIX` | `"session"` | Prefix for the cache entries. |
| `SESSION_TIMEOUT` | `1209600` | Two weeks, in seconds. |
| `SESSION_REFRESH_ON_ACCESS` | `False` | Rewrite a session that was only read, to extend it. |
| `SESSION_COOKIE_NAME` | `"sessionid"` | |
| `SESSION_COOKIE_PATH` | `"/"` | |
| `SESSION_COOKIE_DOMAIN` | `None` | |
| `SESSION_COOKIE_SECURE` | `True` | |
| `SESSION_COOKIE_HTTPONLY` | `True` | |
| `SESSION_COOKIE_SAMESITE` | `"lax"` | |

Give sessions a cache of their own rather than sharing whatever else is being
cached:

```python
CACHES = {
    "default": {"BACKEND": "nitro.cache.backends.MemoryCache", "LOCATION": ""},
    "sessions": {
        "BACKEND": "nitro.cache.backends.RedisCache",
        "LOCATION": "redis://localhost:6379/1",
    },
}
SESSION_CACHE = "sessions"
```

`MemoryCache` is per process, so with more than one worker a session is only
found again by the worker that made it. `nitro check` refuses a deployment
configured that way.

## Sessions are not durable by default

A cache may evict. A session held in one can disappear before it expires,
signing a visitor out or losing a half-finished ceremony such as a passkey
challenge. For state where that is not acceptable, supply a store of your own —
the protocol is five methods:

```python
class DatabaseSessions:
    async def read(self, key: str) -> dict | None: ...
    async def create(self, data: dict, timeout: int) -> str: ...
    async def write(self, key: str, data: dict, timeout: int) -> None: ...
    async def remove(self, key: str) -> None: ...
    async def touch(self, key: str, timeout: int) -> bool: ...


MIDDLEWARE = ["myproject.middleware.DatabaseBackedSessions"]
```

```python
class DatabaseBackedSessions(SessionMiddleware):
    def __init__(self, app=None):
        super().__init__(app, store=DatabaseSessions())
```

`create` allocates the key itself so it can do so atomically. `CacheSessionStore`
uses the cache's `add`, which claims a key and checks it is free in one
operation; a read followed by a write would let two requests be handed the same
key.

## Where the key travels

`read_key` and `write_key` are the seam. By default they use a cookie. An
application whose key lives somewhere else overrides them, and then none of the
`SESSION_COOKIE_*` settings are read:

```python
class TokenSessions(SessionMiddleware):
    def read_key(self, connection):
        return getattr(connection.state, "claims", {}).get("session")

    def write_key(self, response, key):
        pass  # the token already carries it
```

This is the case Nitro will not decide for you, because it changes the security
model. A key in a cookie is *ambient*: the browser attaches it to requests from
any site. A key in a token is not.

## Signing in

Call `cycle()` when a visitor authenticates. It moves the bag to a new key, so
a key an attacker managed to fix beforehand stops being the one the session is
held under:

```python
async def sign_in(request: HttpRequest) -> HttpResponse:
    account = await authenticate(request)
    session = request.state.session

    await session.cycle()
    await session.set("account", account.id)

    return RedirectResponse("/")
```

And `flush()` on the way out, which deletes the session and clears the cookie:

```python
await request.state.session.flush()
```

## Cross-site requests

A cookie-carried session is sent by the browser whether or not the request came
from your site, so install `OriginMiddleware` with it. It checks
`Sec-Fetch-Site`, falling back to `Origin` against `ALLOWED_HOSTS`, on every
method that is not `GET`, `HEAD`, `OPTIONS` or `TRACE`.

There is no token, no secret in the session, no tag to render into a form and
no decorator to exempt a view. That is the point: nothing for an author to
forget, and nothing that needs a form framework to carry it. `nitro check`
reports a cookie-carried session with no origin check installed.

Two limits worth knowing. `same-site` is allowed, so a subdomain is trusted —
right for `app.example.test` calling `api.example.test`, wrong when a subdomain
is under someone else's control. And an unsafe request arriving with neither
header is refused; override `allows` for a caller that genuinely cannot send
one.

## Sockets and WebTransport

`SessionMiddleware` answers for all three protocols. A WebSocket handshake is
an HTTP upgrade, so the cookie arrives and it works as it does for a request:

```python
@app.websocket("/live")
async def live(socket: WebSocket) -> None:
    await socket.accept()
    region = await socket.state.session.get("region")
```

Nothing is written back to the client on these — there is no per-message
response to put a cookie on — but what is in the bag when the handler finishes
is saved, including when the handler ends by raising, which is how a socket
usually ends.

**WebTransport carries no cookies.** The W3C API attaches neither cookies nor
HTTP credentials to the handshake, deliberately, so the default `read_key`
finds nothing there and every browser connection gets an empty session. Two
things work instead.

The key in the URL, which the middleware can read at connect:

```python
class QuerySessions(SessionMiddleware):
    def read_key(self, connection):
        return connection.query_params.get("k")
```

That puts the key in access logs and proxy logs, so it suits a short-lived
handoff token rather than a two-week session key.

Or authenticate over the transport once it is open, which is what the spec
intends and which middleware cannot reach, since it runs before the connection
is up:

```python
@app.webtransport("/live")
async def live(transport: WebTransportSession) -> None:
    await transport.accept()

    token = await transport.receive_datagram()
    transport.state.session = await open_session(await key_for(token))
```

`open_session` is the primitive the middleware itself is built on, which is why
this needs nothing special.

## Long connections

A connection can stay open longer than `SESSION_TIMEOUT`, and then its session
expires underneath it. `touch()` extends the store's copy without rewriting it:

```python
async for message in socket.iter_text():
    await socket.state.session.touch()
    ...
```

## Reference

| | |
|---|---|
| `await session.get(name, default=None)` | |
| `await session.set(name, value)` | |
| `await session.pop(name, default=None)` | |
| `await session.has(name)` | |
| `await session.items()` | The whole bag, as a copy. |
| `await session.clear()` | Empty it, keep the key. |
| `await session.flush()` | Delete it, drop the key. |
| `await session.cycle()` | New key, same contents. |
| `await session.touch()` | Extend it without rewriting. |
| `await session.save(force=False)` | The middleware calls this for you. |
| `session.key` | `None` until there is something to keep. |
| `session.modified` | |
| `session.loaded` | Whether the bag has been read at all. |

A session that was never touched is never read and never written, so a request
that ignores it costs nothing. A key is allocated only when there is something
to keep: putting a value in and taking it out again leaves no session behind.
