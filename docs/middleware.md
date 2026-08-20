# Middleware

Middleware wraps a handler. Each one may implement `__http__`, `__websocket__`
and `__webtransport__`; a middleware that does not implement one is skipped for
that protocol rather than having to pass it through.

```python
import time

from nitro.middleware import Middleware
from nitro.protocols import HttpRequest, HttpResponse


class TimingMiddleware(Middleware):
    async def __http__(self, request: HttpRequest, call_next) -> HttpResponse:
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["x-elapsed-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        return response
```

```python
MIDDLEWARE = [
    "myproject.middleware.TimingMiddleware",
    "nitro.middleware.common.SecurityHeadersMiddleware",
]
```

Outermost first: the first entry sees a request before the second, and its
response last.

Middleware can also be given to the application directly, which is useful in
tests:

```python
app = Nitro(middleware=["myproject.middleware.TimingMiddleware"])
```

## For sockets

```python
from nitro.protocols.websocket import WebSocket


class SocketLogging(Middleware):
    async def __websocket__(self, socket: WebSocket, call_next) -> None:
        logger.info("socket opened on %s", socket.path)
        try:
            await call_next(socket)
        finally:
            logger.info("socket closed on %s", socket.path)
```

A socket handler returns nothing, so socket middleware wraps rather than
transforms.

## What ships

| Middleware | Does |
|---|---|
| `LoggingMiddleware` | Logs each request and how long it took. |
| `CORSMiddleware` | Cross-origin headers, including preflight. |
| `RateLimitMiddleware` | Refuses with 429 past a per-client limit. |
| `ExceptionMiddleware` | Turns an unhandled exception into a 500, and an `HttpException` into the status it names. |
| `SecurityHeadersMiddleware` | `X-Content-Type-Options`, `X-Frame-Options`, HSTS. |
| `OriginMiddleware` | Refuses a state-changing request from another site. |

`ExceptionMiddleware` treats an `HttpException` as an answer rather than a
failure: raising `Http404` gives a 404, not a 500 with a 404 buried in the log.

`OriginMiddleware` checks `Sec-Fetch-Site`, falling back to `Origin` against
`ALLOWED_HOSTS`, on every method that is not `GET`, `HEAD`, `OPTIONS` or
`TRACE`. It is Nitro's answer to cross-site request forgery, and it is stateless:
no token, no secret in the session, nothing to render into a form.

Install it with [`SessionMiddleware`](sessions.md) whenever the session key is
carried in a cookie — the browser attaches that key to requests from any site,
and this is what refuses them. `nitro check` reports the pairing when it is
missing.
