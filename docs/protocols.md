# Requests and responses

A handler receives an `HttpRequest` and returns an `HttpResponse`.

```python
from nitro.protocols import HttpRequest, HttpResponse, JSONResponse


@app.route("/users/<int:user_id>")
async def show_user(request: HttpRequest, user_id: int) -> HttpResponse:
    return JSONResponse({"id": user_id})
```

The base classes carry the protocol in their names because there are three
protocols and the distinction matters at a glance. The specialisations do not
repeat it: `JSONResponse`, not `JsonHttpResponse`.

## Reading a request

```python
async def handler(request: HttpRequest) -> HttpResponse:
    method: str = request.method
    path: str = request.path
    version: str = request.http_version

    agent: str | None = request.headers.get("user-agent")
    page: str = request.query_params.get("page", "1")
    session: str | None = request.cookies.get("session")

    body: bytes = await request.body()
    payload: dict = await request.json()
    fields: FormData = await request.form()
    whatever = await request.data()
```

### Reading a body of more than one kind

`json()` and `form()` each say which format is expected. `data()` is for a
handler that takes more than one and would rather ask once — it parses by what
the request said it was sending:

| Content type | `data()` |
|---|---|
| `application/json`, `*+json` | what the JSON decodes to |
| `application/x-www-form-urlencoded` | `FormData` |
| `multipart/form-data` | `FormData`, uploads included |
| anything else | the bytes that arrived |

```python
async def receive(request: HttpRequest) -> HttpResponse:
    payload = await request.data()
    ...
```

Nothing here guesses at a body whose type it was not told, and a body that does
not parse as what it claimed is a `400`. That last part is where `data()` and
`json()` differ: a caller that named the format itself is handed the decoder's
own error instead, to do with as it likes.

The body is parsed once and remembered, so reading it again through any of these
costs nothing. `request.media_type` is the declared content type without its
parameters, for a handler that would rather branch itself.

### Forms and uploads

`form()` reads both kinds of form — urlencoded and multipart — and which one is
decided by the content type rather than attempted in turn. A body that is not a
form has no fields to give, and parsing one as a form would invent them, so a
JSON request gets an empty form and its body is still there for `json()`.

```python
async def submit(request: HttpRequest) -> HttpResponse:
    form = await request.form()

    title: str = form["title"]
    every: list[str | UploadFile] = form.get_all("tag")

    for name, upload in form.files:
        content: bytes = await upload.read()
```

A file part arrives as an `UploadFile`, with the `filename` and `content_type`
the client declared and the `size` that actually arrived:

```python
avatar = form["avatar"]
data: bytes = await avatar.read()
await avatar.close()
```

The body is parsed as it arrives rather than read whole first, so a file larger
than `MAX_UPLOAD_MEMORY` is written to `UPLOAD_DIR` as it comes in instead of
being held in memory twice. Reading it is blocking I/O and happens in a thread,
which is why `read()` is awaited. What was spooled is deleted when the file is
closed, and `form.close()` closes every upload at once.

A body that claims to be multipart and is not answers `400`, like the other
malformed requests.

`headers` is a mapping that keeps every value a name was sent with:

```python
first: str | None = request.headers.get("accept")
every: list[str] = request.headers.get_all("accept")
```

`len(headers)` counts names, not entries, and iterating yields names — it
behaves like the mapping it looks like. `items()` and `values()` enumerate every
entry, so a name sent twice contributes one key and two items.

### Reading the body as it arrives

```python
async def upload(request: HttpRequest) -> HttpResponse:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
    return JSONResponse({"bytes": total})
```

`body()` reads the whole thing and remembers it, so calling it twice costs one
read. Streaming after reading yields what was read rather than nothing.

### Addresses and state

```python
client: Address | None = request.client       # None on a Unix socket
server: Address | None = request.server
request.state.user = user                     # for middleware and handlers
```

`state` is per request. Nothing on it survives into the next one.

### Noticing the client has gone

```python
async def long_running(request: HttpRequest) -> HttpResponse:
    if request.disconnected:
        return HttpResponse(status_code=499)

    await request.client_disconnect()          # resolves when nobody is left
```

The notification is per connection, and it also fires when the server starts
shutting down. Reacting is the handler's job — nothing is cancelled on its
behalf.

## Sending a response

```python
HttpResponse("plain bytes or text")
HttpResponse({"a": 1})                        # a mapping becomes JSON
HttpResponse(None, status_code=204)

JSONResponse({"a": 1})
PlainTextResponse("hello")
HTMLResponse("<p>hello</p>")
RedirectResponse("/elsewhere")                # 307 by default
```

### Headers and cookies

```python
response = JSONResponse({"ok": True}, headers={"x-request-id": identifier})
response.set_cookie("session", token, httponly=True, samesite="strict")
response.set_cookie("theme", "dark", max_age=31_536_000)
response.delete_cookie("stale")
```

Setting two cookies sends two. They are kept apart from the other headers for
exactly that reason — a mapping would keep only the last.

### Files

```python
from nitro.protocols import FileResponse


@app.route("/download/<path:name>")
async def download(request: HttpRequest, name: str) -> HttpResponse:
    return FileResponse(f"/var/data/{name}", as_attachment=True)
```

The file is read as it is sent, so a large one does not become a large
allocation. Content type and last-modified are filled in from the file unless
you set them.

For a byte range:

```python
return FileResponse(path, range=(start, end))     # end is inclusive, or None
```

### A directory of them

```python
from nitro.staticfiles import StaticFiles

patterns = [
    HTTPRoute("/static/<path:path>", StaticFiles(directory="static"), name="static"),
]
```

`StaticFiles` is an ordinary handler, so it sits in the route table beside
everything else and serves whatever its route captures. It answers `GET` and
`HEAD`, sends an entity tag and a last-modified date, and answers `304` when the
client already holds the version it would send.

A path that climbs out of the directory is a 404, and so is a symlink pointing
out of it unless `follow_symlink=True` says otherwise. A directory is a 403
rather than a listing, since listing publishes names nothing asked to publish.

With `html=True` a directory is served by its `index.html` and a miss by a
`404.html` if there is one, which is what a single-page application needs.

A satisfiable range is answered with `206` and a `Content-Range`. A range
starting past the end of the file is answered with `416`, not with an empty
`206` that would claim the range was honoured. An end past the last byte is
clamped, because asking for more than exists is a normal way to ask for the
rest.

### Streaming

```python
from nitro.protocols import StreamingResponse


@app.route("/events")
async def events(request: HttpRequest) -> HttpResponse:
    async def produce():
        for item in await load_items():
            yield f"data: {item}\n\n"

    return StreamingResponse(produce(), content_type="text/event-stream")
```

Sending waits when the client is behind, so a producer faster than its reader is
slowed rather than filling memory. How far ahead it may run is
`STREAM_QUEUE_CAPACITY`.

### Templates

```python
from nitro.protocols import TemplateResponse


@app.route("/")
async def index(request: HttpRequest) -> HttpResponse:
    return TemplateResponse("index.html", {"user": request.state.user})
```

Rendering happens when the response is written, so middleware can still change
the context after the handler has returned it.

## Exceptions as answers

```python
from nitro.protocols import Http404, HttpForbidden


async def show(request: HttpRequest, user_id: int) -> HttpResponse:
    user = await find_user(user_id)
    if user is None:
        raise Http404()
    if not user.visible_to(request.state.viewer):
        raise HttpForbidden({"reason": "not yours"})
    return JSONResponse(user.as_dict())
```

Raising one of these is a way of saying "answer with this status" without
building the response. A string detail becomes plain text; anything else becomes
JSON. They are answers, not failures, and are never flattened into a 500.

### Answering a status yourself

`exception_handlers` maps a status code or an exception class to the handler
that answers it. It lives beside `patterns` in the route module, so a project's
error pages are declared where its routes are:

```python
# myproject/routes.py
from nitro.protocols import HttpForbidden

patterns = [...]

exception_handlers = {
    404: "myproject.views.not_found",     # by status, named to avoid the import
    500: server_error,                    # or the callable itself
    HttpForbidden: forbidden,             # or by exception class
}
```

or given to the application, which overrides what the route module declares:

```python
app = Nitro(exception_handlers={404: not_found})
```

A handler takes the request and the exception, and returns a response:

```python
async def not_found(request: HttpRequest, exception: Exception) -> HttpResponse:
    return TemplateResponse("errors/404.html", {"path": request.path}, status_code=404)
```

A key is looked up by exact exception type, then by status for an
`HttpException`, then up the class hierarchy — so `HttpException` catches every
status a handler has not claimed for itself. An ordinary exception carries no
status of its own, and the answer it becomes is a 500, so `500` is the key it
reaches.

Handlers cover WebSocket and WebTransport failures too; there the handler is
given the socket or session rather than a request, and returns nothing.

A path that matched no route has no request of its own, so one is built for the
handler — a 404 page is the one most likely to want the path it was asked for.
A handler that raises is logged and treated as absent: the client is owed an
answer for the original failure, not for the one raised while describing it.

Only the route module named by `ROUTES` is read. A `Mount` does not carry its
own, because an unmatched path belongs to no route and so to no mount.

### While DEBUG is on

A 404 is answered with a page listing the routes that were tried, and a 500 with
the traceback and the source around each frame. Both come from templates Nitro
carries rather than the project's own engine, since they have to work when the
project's configuration is what is broken. A handler registered for the status
wins over either, so a project's own page is what you see whether or not
`DEBUG` is on.

Every other status keeps its ordinary answer — a 403 stays the response the
exception describes. With `DEBUG` off, a 404 is `Not Found` and a 500 is
`Internal Server Error`, and nothing about the failure reaches the client.

The `DEBUG` setting is where this comes from unless the application was told
directly:

```python
app = Nitro(debug=True)
```

which is what a test wants when the project it runs against is configured for
production. The setting is read on each request rather than at construction, so
changing it takes effect without rebuilding the application.

## Answering directly

A handler that wants the transport can reach it and return nothing:

```python
async def custom(request: HttpRequest) -> None:
    request.protocol.response_bytes(200, [("content-type", "text/plain")], b"raw")
```

This is the low-level surface the response classes are built on. Reach for it
when you need something they do not offer, not by default.
