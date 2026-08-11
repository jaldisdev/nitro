# Changelog

Notable changes to Nitro. The format follows [Keep a Changelog][keepachangelog],
and this project adheres to [Semantic Versioning][semver] — with the usual
pre-1.0 caveat that minor versions may still break things.

[keepachangelog]: https://keepachangelog.com/en/1.1.0/
[semver]: https://semver.org/spec/v2.0.0.html

## [Unreleased]

### Added

- `ALLOWED_HOSTS` is now enforced. The compiled server checks every request's
  `Host` against it and answers `400` before the request reaches the
  application. An empty list still answers for any name, and `nitro check`
  fails a deployment that leaves it that way with `DEBUG` off.
- Intercom backends are a real choice: `INTERCOMS[...]["BACKEND"]` is read and
  dispatched on. `MemoryIntercom` is the new default and keeps everything in
  one process, so Intercom works before a project has a Redis;
  `RedisIntercom` is what a deployment with more than one worker needs.
- Cache values are serialized with a configurable `SERIALIZER`, defaulting to
  JSON. `pickle` remains available as an explicit opt-in.
- `nitro.utils.datetime` gained `activate`, `deactivate`, `override` and
  `localtime`.
- `nitro.utils.crypto.constant_time_compare`, which `nitro.utils.tokens`
  imported but which had never existed.

### Changed

- **Breaking.** The `SERVER` settings mapping is gone. Its keys are now flat
  top-level settings prefixed with `SERVER_` — `SERVER_PORT`, `SERVER_WORKERS`,
  `SERVER_TLS_CERT` and so on — matching how every other subsystem's settings
  are named. Nothing reads `SERVER` any more, so a project that still defines
  it is running on the defaults.
- `BaseEmailBackend` no longer takes SMTP-shaped constructor arguments. A
  backend declares the settings it wants in a `settings_map`, and
  `get_connection` reads that instead of matching on the import path.
- `send_messages(fail_silently=...)` no longer writes the flag onto the
  backend, so one caller's choice cannot leak into another's.
- `nitro.protocols` exports the WebSocket and WebTransport classes and the full
  set of HTTP exceptions.
- `BaseStorage.get_accessed_time` is no longer abstract; a backend that cannot
  answer raises `StorageOperationUnsupported`.

### Fixed

- Middleware no longer disappears silently. The stack decided whether a hook
  was implemented by calling it and catching `AttributeError` and
  `NotImplementedError`, so any such error raised *inside* a middleware looked
  like the middleware not being there and the connection was served as though
  it were not installed.
- `LoggingMiddleware` read the request scope as a dictionary, which the
  compiled scope is not. It raised on every connection and was then silently
  skipped by the bug above, so it never logged anything.
- `WebSocketEndpoint` and `WebTransportEndpoint` called three methods that do
  not exist — `websocket.iter`, `session.iter_datagrams(encoding)` and
  `session.receive_stream` — and failed on first use.
- `MemcachedCache` was written against an emcache that does not exist: it
  built the client synchronously with the wrong constructor, read `Item`s as
  though they were bytes, expected booleans from calls that signal by raising,
  called a `set_many` the client has no such method for, and called
  `flush_all` without the node it requires.
- `RedisCache.add` returned `None` instead of `False` when the key was already
  present.
- `to_camel_case`, `to_snake_case` and `get_current_timezone` raised
  `NameError` on every call.
- A server bound with `SERVER_PORT = 0` could fail to start when the port the
  kernel chose for the first socket was taken on another address or on UDP.
  Binding now retries on a fresh port.
- A metric whose descriptor cannot be built no longer aborts the worker.

### Security

- Host header validation, as above. Before this, `ALLOWED_HOSTS` was declared,
  documented and checked by `nitro check` while being read by nothing.
- Cache backends no longer default to pickle, which runs code contained in the
  data. See [SECURITY.md](SECURITY.md).
