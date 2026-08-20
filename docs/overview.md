# Nitro

Nitro is an async-first Python web framework with its server compiled in.

## Not an ASGI framework

Nitro does not implement ASGI and does not ship an adapter for it. The server —
HTTP/1.1, HTTP/2, HTTP/3, WebSocket and WebTransport — is part of the package
and calls your application directly.

This is a design choice, and it is the one the rest of the framework is built
on. An intermediate protocol constrains what a server can offer to whatever
that protocol can express, and pays for every request with a translation into
dictionaries and callables. Removing it is what lets routing, header handling,
file serving, range requests and streaming backpressure live in compiled code
while your handlers stay ordinary Python coroutines.

The trade is real and worth stating plainly: a Nitro application runs on the
Nitro server. You cannot deploy it under Uvicorn, Hypercorn or Daphne, and you
cannot mount an ASGI application inside it. If you need either of those, Nitro
is the wrong choice.

## An application

```python
# myproject/views.py
from nitro.protocols import HttpRequest, HttpResponse, JSONResponse, PlainTextResponse


async def index(request: HttpRequest) -> HttpResponse:
    return PlainTextResponse("hello")


async def show_user(request: HttpRequest, user_id: int) -> HttpResponse:
    return JSONResponse({"id": user_id, "name": "Ada"})
```

```python
# myproject/routes.py
from nitro.routing import HTTPRoute

from myproject.views import index, show_user

patterns = [
    HTTPRoute("/", index, name="index"),
    HTTPRoute("/users/<int:user_id>", show_user, name="user"),
]
```

```python
# myproject/settings.py
ROUTES = "myproject.routes"
```

```python
# myproject/main.py
from nitro import Nitro

app = Nitro()
```

```sh
NITRO_SETTINGS_MODULE=myproject.settings nitro myproject.main:app
```

A handler receives a [`HttpRequest`](protocols.md) and returns a `HttpResponse`. Path
parameters arrive as keyword arguments, already converted — `user_id` above is
an `int`, not the string `"42"`.

Routes can also be registered on the application with `@app.route(...)`, which
adds to the configured table. See [routing](routing.md) for both.

## How a request is served

1. A worker accepts the connection and reads the request.
2. The compiled matcher finds the route, checks each captured parameter against
   the expression its converter supplies, and attaches the result to the scope.
3. The application turns the captured text into Python values, builds a
   `HttpRequest`, and runs it through the middleware stack.
4. The handler returns a response, which is written back through the transport.

Steps 1 and 2 are compiled. Steps 3 and 4 are yours.

## What is in the box

| Area | Where |
|---|---|
| Routing, converters, mounting | [routing.md](routing.md) |
| Requests, responses, exceptions | [protocols.md](protocols.md) |
| WebSocket and WebTransport | [realtime.md](realtime.md) |
| Publish/subscribe between connections | [intercom.md](intercom.md) |
| Settings and the server's own options | [settings.md](settings.md) |
| Dependency injection | [di.md](di.md) |
| Caching, storage, mail, templates | [cache.md](cache.md), [storage.md](storage.md), [mail.md](mail.md), [templates.md](templates.md) |
| Middleware | [middleware.md](middleware.md) |
| Sessions and cross-site requests | [sessions.md](sessions.md) |
| Prometheus metrics | [observability.md](observability.md) |
| The command line | [cli.md](cli.md) |
| Running in production | [deployment.md](deployment.md) |

## Requirements

Python 3.13 or newer, installed with `pip install nitro-framework`. The
distribution is named `nitro-framework`; the package it installs is `nitro`.
The wheel carries the compiled server; there is nothing else to install to
serve traffic. Optional extras pull in the clients particular backends need —
`nitro-framework[redis]`, `nitro-framework[aws]`, `nitro-framework[azure]`,
`nitro-framework[sendgrid]`, `nitro-framework[memcached]`,
`nitro-framework[email-oauth]`, or `nitro-framework[all]`.
