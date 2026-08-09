# Routing

Routes are declared in Python and matched in compiled code. What crosses
between them at startup is a description of each route: its path, the methods it
answers, and for every parameter the expression that recognises it.

## The route table

A project's routes live in a module of their own, in a list called `patterns`.
`ROUTES` says where that module is.

```python
ROUTES = "myproject.routes"
```

```python
# myproject/routes.py
from nitro.routing import HTTPRoute, Mount, WebSocketRoute, WebTransportRoute

from myproject.views import UserEndpoint, about, room

patterns = [
    HTTPRoute("/about", about, name="about"),
    HTTPRoute("/users/<int:user_id>", UserEndpoint, name="user"),
    HTTPRoute("/things", things, methods=["GET", "POST"], name="things"),
    WebSocketRoute("/rooms/<slug:room>", room, name="room"),
    Mount("/api", api_patterns, name="api"),
]
```

The application reads it as it is constructed:

```python
app = Nitro()                          # ROUTES
app = Nitro(routes="myproject.routes") # or named directly
app = Nitro(routes=patterns)           # or handed the declarations
```

A declaration is a description, not a registration. Writing the table in a
module of its own is what lets it be imported and read on its own — by a test,
by a command, by anything that wants to know what a project serves without
starting it.

A route may be named, which is what makes it reversible.

### What a route answers

`methods` defaults to what the handler can answer: `GET` and `HEAD` for a
function, and for an endpoint class the verbs it defines.

```python
class UserEndpoint(HTTPEndpoint):
    async def get(self, request: HttpRequest, user_id: int) -> HttpResponse: ...
    async def post(self, request: HttpRequest, user_id: int) -> HttpResponse: ...
```

`HTTPRoute("/users/<int:user_id>", UserEndpoint)` therefore answers `GET`,
`HEAD` and `POST`. This matters because methods are checked in the compiled
matcher: a verb the route did not declare is a 405 that never reaches the
endpoint. Give `methods` explicitly to narrow that.

An endpoint class is instantiated once per request, so it may keep state on
`self` for the duration of a call without it leaking into the next one.

## Decorators

Handlers can also be registered on the application directly. This adds to the
configured table rather than replacing it, and is meant for a small application
or a test rather than as the way a project declares its routes.

```python
from nitro.protocols import HttpRequest, HttpResponse, PlainTextResponse


@app.route("/about")
async def about(request: HttpRequest) -> HttpResponse:
    return PlainTextResponse("about")


@app.route("/things", methods=["GET", "POST"])
async def things(request: HttpRequest) -> HttpResponse:
    ...


app.add_route("/other", handler, methods=["GET"], name="other")
```

## Path parameters

A parameter is written `<converter:name>`, or `<name>` for the default:

```python
@app.route("/users/<int:user_id>/posts/<slug:title>")
async def post(request: HttpRequest, user_id: int, title: str) -> HttpResponse:
    ...          # already converted; user_id is an int, not "42"
```

| Converter | Accepts | Produces |
|---|---|---|
| `str` (default) | anything but `/` | `str` |
| `int` | digits | `int` |
| `slug` | letters, digits, `-`, `_` | `str` |
| `uuid` | a hyphenated UUID | `uuid.UUID` |
| `path` | anything, including `/` | `str` |

`path` may only be the last thing in a path, since it consumes the rest of it.

An expression can also be written inline:

```python
@app.route('/assets/<regex("[a-z]{2}"):language>.json')
async def asset(request: HttpRequest, language: str) -> HttpResponse:
    ...
```

### Custom converters

```python
from nitro.routing import Converter, register_converter


class HexConverter(Converter):
    regex = "[0-9a-f]+"

    def to_python(self, value: str) -> int:
        return int(value, 16)

    def to_url(self, value: int) -> str:
        return format(value, "x")


register_converter("hex", HexConverter)
```

Only `regex` crosses into the matcher. `to_python` runs afterwards, in Python,
so a converter can produce whatever it likes — the matcher never needs to know
what a UUID is.

If `to_python` rejects a value the matcher accepted, the request is a 404: the
path did not name anything, which is what the client needs to be told.

## How matching works

Two steps. A radix tree finds the routes whose *shape* fits the path, which is
the part that runs on every request. The candidates it returns are then checked
against what each of their parameters actually accepts, in registration order,
and the first that passes wins.

Two things follow from this that are worth knowing:

- **Routes that differ only in converter can coexist.** `/things/<int:id>` and
  `/things/<slug:name>` have the same shape; their expressions tell them apart.
- **A static segment beats a parameter.** `/users/new` wins over
  `/users/<str:name>`, because the tree sees them as different shapes.

Where two routes genuinely overlap — `/things/<str:anything>` registered before
`/things/<int:id>` — the earlier one wins. Register the narrower route first.

## Methods

`HEAD` is answered by whatever answers `GET`; a response to one is a response to
the other with the body left off, and the transport does the leaving off.

A path that exists but not for the method used is a 405 with an accurate
`Allow` header, built from the routes that are registered.

## Mounting

A `Mount` re-registers another table's routes under a prefix, which is how a
project splits its routes across modules:

```python
# myproject/api/routes.py
patterns = [
    HTTPRoute("/status", status, name="status"),
]
```

```python
# myproject/routes.py
from myproject.api.routes import patterns as api_patterns

patterns = [
    Mount("/api", api_patterns, name="api"),
]
```

`/api/status` now serves it, and the route is reversible as `api:status` — the
mount's name is a namespace for the names inside it.

A `Router` built with decorators can be mounted the same way:

```python
from nitro.routing import Mount, Router

api = Router()


@api.route("/status")
async def status(request: HttpRequest) -> HttpResponse:
    return JSONResponse({"ok": True})


app.mount(Mount("/api", api, name="api"))
```

A mount is a grouping device and nothing more. There is no notion of handing a
request off to a separate application — everything a Nitro project serves is
served by the same application object.

Mounts nest.

## Reversing

```python
path: str = app.url_for("post", user_id=42, title="hello")
# "/users/42/posts/hello"
```

or, from anywhere:

```python
from nitro.shortcuts import reverse

path: str = reverse("post", user_id=42, title="hello")
```

`reverse` looks in the application constructed most recently, which in a worker
is the only one there is. Values are turned back into path text by each
parameter's converter, so a `uuid.UUID` reverses to the string it came from.

## WebSocket and WebTransport routes

They live in the same table, registered under methods no HTTP request can carry:

```python
from nitro.routing import WebSocketRoute, WebTransportRoute

patterns = [
    WebSocketRoute("/rooms/<slug:room>", room, name="room"),
    WebTransportRoute("/live", live, name="live"),
]
```

or, on the application:

```python
from nitro.protocols.websocket import WebSocket
from nitro.protocols.webtransport import WebTransportSession


@app.websocket("/rooms/<slug:room>")
async def room(socket: WebSocket, room: str) -> None:
    await socket.accept()


@app.webtransport("/live")
async def live(session: WebTransportSession) -> None:
    await session.accept()
```

One path can carry all three protocols at once.
