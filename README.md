# Nitro

An async-first Python web framework with its server compiled in.

```python
from nitro import Nitro
from nitro.protocols import HttpRequest, HttpResponse, JSONResponse

app = Nitro()


@app.route("/users/<int:user_id>")
async def show_user(request: HttpRequest, user_id: int) -> HttpResponse:
    return JSONResponse({"id": user_id, "name": "Ada"})
```

```sh
nitro run app:app
```

## Not an ASGI framework

Nitro does not implement ASGI and does not ship an adapter for it. The server —
HTTP/1.1, HTTP/2, HTTP/3, WebSocket and WebTransport — is part of the package
and calls your application directly.

That is a design choice rather than an omission. An intermediate protocol limits
a server to what the protocol can express and charges every request for a
translation into dictionaries and callables. Removing it is what lets routing,
header handling, file serving, range requests and streaming backpressure live in
compiled code while handlers stay ordinary Python coroutines.

The trade is worth stating plainly: a Nitro application runs on the Nitro
server. It cannot be deployed under Uvicorn or Hypercorn, and it cannot mount an
ASGI application inside itself.

## Documentation

Start with [the overview](docs/overview.md).

| | |
|---|---|
| [Routing](docs/routing.md) | Paths, converters, mounting, reversing |
| [Requests and responses](docs/protocols.md) | Reading a request, sending files and streams |
| [WebSocket and WebTransport](docs/realtime.md) | Real-time connections |
| [Intercom](docs/intercom.md) | Publish/subscribe between connections |
| [Settings](docs/settings.md) | Configuration, including the server's own |
| [Dependency injection](docs/di.md) | `Depends`, and why its cache is per request |
| [Middleware](docs/middleware.md) | Wrapping handlers |
| [Caching](docs/cache.md) · [Storage](docs/storage.md) · [Templates](docs/templates.md) · [Mail](docs/mail.md) | Batteries |
| [Command line](docs/cli.md) | `nitro run`, `check`, `shell` |
| [Deployment](docs/deployment.md) | Workers, TLS, HTTP/3, draining |

## Layout

| Path | Contents |
|---|---|
| `nitro/` | The framework |
| `nitro-intercom/` | Standalone publish/subscribe client for non-Nitro services |
| `crates/nitro-core/` | Transport, lifecycle and routing — pure Rust, no interpreter |
| `crates/nitro-py/` | Python bindings for the server (`nitro._nitro`) |
| `crates/intercom-core/` | Publish/subscribe channels — pure Rust, no interpreter |
| `crates/intercom-py/` | Python bindings for Intercom (`nitro_intercom._intercom`) |

The two core crates know nothing about Python. Everything that needs an
interpreter goes through a trait the binding crate implements, which is what
keeps the whole request path testable without one.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install maturin
maturin develop --extras dev          # builds the extension into the venv
```

```sh
cargo test --workspace                # Rust
pytest                                # Python
(cd nitro-intercom && pytest)         # the standalone package
```

Tests that need Redis skip when there is not one reachable at
`127.0.0.1:6379`, so a checkout without it still runs green.

## Requirements

Python 3.13 or newer. The wheel carries the compiled server. Optional extras
pull in the clients particular backends need: `nitro[redis]`, `nitro[aws]`,
`nitro[azure]`, `nitro[sendgrid]`, `nitro[memcached]`, `nitro[email-oauth]`, or
`nitro[all]`.

## License

MIT OR Apache-2.0
