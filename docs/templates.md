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

An engine can keep compiled bytecode in one of the project's caches, which
avoids recompiling on every worker start:

```python
TEMPLATE_CACHE = "default"
```
