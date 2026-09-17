# Templates

Jinja2, rendered asynchronously.

```python
TEMPLATES = [
    {
        "BACKEND": "nitro.templates.engine.Jinja2",
        "NAME": "default",
        "DIRS": ["templates"],
        "APP_DIRS": True,
        "OPTIONS": {"autoescape": True},
    }
]
```

```python
from nitro.protocols import HttpRequest, HttpResponse, TemplateResponse


@app.route("/")
async def index(request: HttpRequest) -> HttpResponse:
    return TemplateResponse("index.html", {"title": "Home"})
```

Rendering happens when the response is written rather than when it is built, so
middleware can still change the context after a handler has returned it.

## Rendering directly

```python
from nitro.templates import templates

template = templates.get_template("mail/welcome.html")
body: str = await template.render_to_string({"user": user})

body = await templates.render_to_string("mail/welcome.html", {"user": user})
```

There is a synchronous `render_to_string_sync` for code that is not async. It
cannot be called from inside a running event loop, and says so if you try.

## Several engines

```python
TEMPLATES = [
    {"NAME": "web", "DIRS": ["templates/web"], "OPTIONS": {"autoescape": True}},
    {"NAME": "mail", "DIRS": ["templates/mail"], "OPTIONS": {"autoescape": False}},
]
```

```python
templates.get_template("welcome.html", using="mail")
TemplateResponse("index.html", context, using="web")
```

Without `using`, the first configured engine is used.

## Caching compiled templates

Compiled bytecode can be kept in one of the project's caches, so a worker that
starts does not compile every template again:

```python
TEMPLATE_CACHE = "default"
```

It is off unless set, and it needs a cache every worker reaches: a
`MemoryCache` only holds what the process that compiled it already has.

Jinja reads bytecode synchronously while it loads a template, and a cache is
reached with an await, so each process works from its own copy. Before its first
render it reads the bytecode of every template the engine can list — not just the
one being rendered, since those it extends or includes are loaded mid-render —
and after each render it stores whatever was compiled. A changed template no
longer matches the checksum stored with its bytecode and is compiled afresh.

A cache that cannot be reached is logged and rendering carries on, compiling as
it would without one. An engine's own `OPTIONS["bytecode_cache"]`, such as
Jinja's `FileSystemBytecodeCache`, takes precedence over the setting.
