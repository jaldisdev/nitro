# Routing

Routes are declared in Python and matched in compiled code. What crosses
between them at startup is a description of each route: its path, the methods it
answers, and for every parameter the expression that recognises it.

## Declaring routes

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

A route may be named, which is what makes it reversible.

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

A `Mount` re-registers another router's routes under a prefix:

```python
from nitro.routing import Mount, Router

api = Router()


@api.route("/status")
async def status(request: HttpRequest) -> HttpResponse:
    return JSONResponse({"ok": True})


app.mount(Mount("/api", api, name="api"))
```

`/api/status` now serves it, and the route is reversible as `api:status`.

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
