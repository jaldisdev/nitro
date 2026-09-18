# Changelog

Notable changes to Nitro. The format follows [Keep a Changelog][keepachangelog],
and this project adheres to [Semantic Versioning][semver] — with the usual
pre-1.0 caveat that minor versions may still break things.

[keepachangelog]: https://keepachangelog.com/en/1.1.0/
[semver]: https://semver.org/spec/v2.0.0.html

## [Unreleased]

## [0.2.0] - 2026-09-18

### Added

- `nitro static collect`, which copies every file in `STATIC_DIRS` into
  `STATIC_ROOT` for a deployment to serve. A file is copied only when the
  destination is missing or differs in size or modification time, the first
  directory listed wins a path two of them hold, and `--clear` empties the
  destination first. Command discovery now leaves a group's subcommands under
  their group instead of registering each of them at the top level as well.

- `nitro.testing.TestClient`, which drives an application in-process over
  HTTP, WebSocket and WebTransport. Routes are found by the compiled matcher,
  now also available on its own as `nitro._nitro.RouteMatcher`, and file
  responses are answered by the server's file code, so neither is
  reimplemented for tests. Cookies persist across requests, `stream()` reads a
  response while it is produced, and `async with` runs the startup and shutdown
  work a worker does.
- `TEMPLATE_CACHE` now does what the documentation said: compiled template
  bytecode is kept in the named cache, so a worker that starts renders without
  compiling again. Each process serves Jinja from its own copy, read from the
  cache before its first render and written back after each render, because
  Jinja loads bytecode synchronously and a cache is reached with an await.
- `LOGGING`, a `dictConfig` mapping for Python logging, merged by name over
  defaults that write the `nitro` logger to stderr in the server log's text
  layout. It is applied by the command line and by `app.serve()`, never on
  import. A setting that cannot be applied falls back to the defaults with a
  warning rather than stopping the server, and `nitro check` reports it. The
  server's own log is still configured by `SERVER_LOG_*` alone.
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
- `nitro.utils.crypto.constant_time_compare`, which the package referred to but
  had never defined.
- `nitro.utils.http`, with `patch_vary_headers` and
  `content_disposition_header`. Middleware each know one thing a response
  varies on and none of them knows the others, so `Vary` has to be merged
  rather than assigned; and a `Content-Disposition` filename needs RFC 6266
  encoding to survive a name outside ASCII.
- `nitro.sessions`: server-side state keyed to a connection, kept in the cache
  named by `SESSION_CACHE` and configured with the flat `SESSION_*` settings.
  `SessionMiddleware` leaves it at `request.state.session` and answers for all
  three protocols. Every operation is a coroutine and the bag is read once per
  connection, so a request that never touches the session costs nothing and one
  that reads ten keys costs one round trip.

  Where the key travels is the application's decision: `read_key` and
  `write_key` carry it in a cookie by default and are meant to be overridden by
  a project whose key rides in a token or a header. `open_session` is the
  primitive the middleware is built on and is public, because a WebTransport
  connection can only authenticate after it is up, which middleware cannot
  reach. Sessions are held somewhere else entirely by supplying a
  `SessionStore` — five methods — which is also what a deployment does when
  cache eviction losing a session is not acceptable.

  There is no login, no identity and no device registry. `Session.cycle()`
  exists because session fixation has to be defended against at sign-in, and
  only the application knows when that is.
- `OriginMiddleware`, Nitro's answer to cross-site request forgery. It checks
  `Sec-Fetch-Site`, falling back to `Origin` against `ALLOWED_HOSTS`, on every
  unsafe method — no token, no secret in the session, no tag to render into a
  form and no decorator to exempt a view. Ships alongside sessions rather than
  separately: once Nitro sets a session cookie it is issuing a credential the
  browser attaches to requests from anywhere, so the check stops being optional.
- `nitro check` reports two ways sessions go wrong: kept in a `MemoryCache`
  with more than one worker, where only the worker that made a session finds it
  again; and carried in a cookie with no origin check installed.

### Changed

- `SESBackend` calls the SES v2 API (`sesv2`, `SendEmail`) instead of v1
  (`ses`, `SendRawEmail`). Messages still go as raw MIME and the `EMAIL_AWS_*`
  settings are unchanged, but sending now needs the `ses:SendEmail` IAM
  permission.
- The route matcher is Nitro's own rather than `matchit`. A path segment can
  now hold text and several parameters, such as `photo_<int:id><size>.<ext>`,
  and a segment with text before a parameter can sit beside a bare parameter in
  the same position, which `matchit` refused at startup. A segment an
  expression rejects falls through to routes registered after it, and a route
  that answers the method now wins over a more specific one that does not,
  instead of the request being a 405.
- **Breaking.** `TEMPLATE_CACHE` defaults to `None`, turning bytecode caching
  on only when it names a cache. `MemcachedBytecodeCache` is gone: it read a
  Memcached client attribute no Nitro cache backend has, so it could not be
  constructed against one.
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
- **Breaking.** The dependency-injection context parameter `session` is now
  `transport`. It names the WebTransport session, and that reading only got
  more confusing once a connection started carrying a session store as well —
  `session.state.session` was about to become idiomatic. A handler is unaffected,
  because it receives its connection positionally; a *dependency* that asked for
  the WebTransport session by naming a parameter `session` has to rename it.
- `nitro.protocols` exports the WebSocket and WebTransport classes and the full
  set of HTTP exceptions.
- `BaseStorage.get_accessed_time` is no longer abstract; a backend that cannot
  answer raises `StorageOperationUnsupported`.
- The `aws` extra installs `aiobotocore` rather than `aioboto3`, which `S3Storage`
  and `SESBackend` now use directly. aioboto3 pins one exact aiobotocore release,
  so installing the extra used to downgrade `boto3` and `aiobotocore` in any
  project that already depended on newer ones.

### Fixed

- A template engine's `OPTIONS["autoescape"]` was ignored and autoescaping
  was always on, so the documented plain-text mail engine escaped its output.
- The mail documentation described APIs that do not exist: `send_email` with
  `body`/`to`, `EmailMessage.attach(name, content, mimetype)`,
  `attach_alternative`, `send_mass_email` taking messages, and an
  `OAuthSMTPBackend`. It now shows `message`/`recipient_list`,
  `attach(EmailAttachment(...))`, the `html` attribute, the tuples
  `send_mass_email` takes, and `OAuth2SMTPBackend`.
- `S3Storage.close()` awaited a `close()` the aioboto3 session never had, so
  `storages.close_all()` raised `AttributeError` for any project with an S3
  storage.
- `FileResponse` built its `Content-Disposition` by interpolating the filename
  into a quoted string, so a name containing a quote or backslash truncated the
  header and a name outside ASCII arrived mangled. It now goes through
  `nitro.utils.http.content_disposition_header`.
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
