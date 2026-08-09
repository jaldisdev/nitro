# Nitro

An async-first Python web framework with a bundled Rust server.

Nitro is not an ASGI framework and does not ship an ASGI adapter. The server —
HTTP/1.1, HTTP/2, HTTP/3, WebSocket and WebTransport — is compiled into the
package and speaks directly to your application object. That is a deliberate
design choice: dropping the intermediate protocol layer is what lets request
routing, header handling, file serving and streaming backpressure live in Rust
while your handlers stay ordinary Python coroutines.

## Layout

| Path | Contents |
|---|---|
| `nitro/` | The framework: application, routing, DI, settings, cache, storage, mail, templates, CLI |
| `nitro_intercom/` | Standalone publish/subscribe client for non-Nitro Python services |
| `crates/nitro-core/` | Transport, lifecycle and routing core — pure Rust, no interpreter |
| `crates/nitro-py/` | Python bindings for the server core (`nitro._nitro`) |
| `crates/intercom-core/` | Publish/subscribe channels — pure Rust, no interpreter |
| `crates/intercom-py/` | Python bindings for Intercom (`nitro_intercom._intercom`) |

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install maturin
maturin develop --extras dev     # builds the Rust extension into the venv
pytest                           # Python tests
cargo test --workspace           # Rust tests
```

## License

MIT OR Apache-2.0
