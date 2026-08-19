# Settings

Defaults are always loaded. The module named by `NITRO_SETTINGS_MODULE`, if set,
overrides them. Only names in upper case are read.

```sh
export NITRO_SETTINGS_MODULE=myproject.settings
```

```python
from nitro.settings import settings

if settings.DEBUG:
    ...
```

Resolution is deferred until the first attribute is read, so importing `nitro`
never depends on a project being configured.

## The server

The bundled server's options are flat top-level settings. Anything left out
keeps its default.

```python
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8000
SERVER_WORKERS = 4
SERVER_HTTP = "auto"
SERVER_TLS_CERT = "/etc/tls/site.pem"
SERVER_TLS_KEY = "/etc/tls/site.key"
SERVER_ACCESS_LOG = True
```

Flat rather than nested under a `SERVER` mapping, for the same reason the
observability options are: there is exactly one server to configure, and a
mapping would suggest several named ones can be. Only `CACHES`, `STORAGES` and
`INTERCOMS` are mappings, because each of those really does hold several named
entries.

Prefixed the way every subsystem's flat settings are — `EMAIL_HOST` for mail,
`SECURE_HSTS_SECONDS` for the security headers, `SERVER_PORT` for the server. A
settings module is one namespace shared with everything a project configures
for itself, so a bare `PORT` or `WORKERS` there belongs to whoever thought of
it first.

| Setting | Default | Meaning |
|---|---|---|
| `SERVER_HOST`, `SERVER_PORT` | `localhost`, `8000` | Where to listen. Every address the host resolves to gets a socket. |
| `SERVER_UDS` | `None` | A Unix socket instead of a port. |
| `SERVER_HTTP` | `"auto"` | Highest version to negotiate: `"1"`, `"2"`, `"3"`, or `"auto"` for the highest available. |
| `SERVER_WEBSOCKETS`, `SERVER_WEBTRANSPORT` | `True` | Whether upgrades are honoured. WebTransport needs HTTP/3 and switches off without it. |
| `SERVER_TLS_CERT`, `SERVER_TLS_KEY` | `None` | Required whenever HTTP/3 is active. |
| `SERVER_TLS_CA`, `SERVER_TLS_CLIENT_AUTH` | `None`, `"none"` | Client certificates: `"none"`, `"optional"` or `"required"`. |
| `SERVER_TLS_TCP` | `True` | Terminate TLS on the TCP socket. Off when a proxy already does. |
| `SERVER_TLS_RELOAD_INTERVAL` | `10.0` | Seconds between certificate checks. `0` disables reloading. |
| `SERVER_WORKERS`, `SERVER_RUNTIME_THREADS` | `1`, `2` | Processes, and threads within each one's runtime. |
| `SERVER_BACKLOG` | `1024` | Kernel accept queue depth. |
| `SERVER_MAX_CONCURRENT_CONNECTIONS` | `None` | Per-worker cap. Beyond it, connections wait in the backlog. |
| `SERVER_DATAGRAM_QUEUE_CAPACITY` | `64` | Datagrams held per session before the oldest is dropped. |
| `SERVER_STREAM_QUEUE_CAPACITY` | `16` | How far a streaming response may run ahead of its client. |
| `SERVER_ALT_SVC` | `"auto"` | Advertising HTTP/3: `"auto"`, `"off"`, or a verbatim header value. |
| `SERVER_DRAIN_TIMEOUT` | `30.0` | Seconds in-flight work gets once shutdown starts. |
| `SERVER_HEADER` | `"nitro"` | `None` omits the header. |
| `SERVER_LOG_LEVEL`, `SERVER_LOG_DESTINATION`, `SERVER_LOG_FORMAT` | `"info"`, `"stderr"`, `"pretty"` | The server log. A destination other than `stdout`/`stderr` is a file path. |
| `SERVER_ACCESS_LOG`, `SERVER_ACCESS_LOG_DESTINATION`, `SERVER_ACCESS_LOG_FORMAT` | `False`, `"stdout"`, `"combined"` | The access log, configured separately. |

### Where a value comes from

Three places, in increasing order of precedence:

1. the settings module;
2. keyword arguments to `Nitro(...)`, which are these names without the
   `SERVER_` prefix and in lower case — `Nitro(port=9000)` for `SERVER_PORT`;
3. command line flags.

```python
app = Nitro(port=9000, workers=2)
```

```sh
nitro app:app --port 9500          # 9500 wins
```

A flag that was not given is dropped rather than applied, so it cannot erase a
constructor argument.

## Everything else

| Setting | Purpose |
|---|---|
| `DEBUG` | Detail in error responses, including the 404 and 500 pages. `Nitro(debug=...)` overrides it. Never on in production. |
| `RELOAD` | Restart the server when a `.py` file changes. Development only; `--reload` turns it on regardless. See [the CLI](cli.md#reloading-during-development). |
| `SECRET_KEY` | Signing. Required outside debug. |
| `SECRET_KEY_FALLBACKS` | Older keys still accepted when checking a signature, so `SECRET_KEY` can be rotated. |
| `ALLOWED_HOSTS` | Host names this site answers for. See below. |
| `MIDDLEWARE` | Import paths, outermost first. See [middleware](middleware.md). |
| `CORS_*` | Read by `CORSMiddleware`. See [middleware](middleware.md). |
| `SECURE_*` | Read by `SecurityHeadersMiddleware`. See [middleware](middleware.md). |
| `COMMAND_MODULES` | Packages searched for extra CLI commands. |
| `ROUTES` | The module holding the route table, which defines `patterns`. See [routing](routing.md). |
| `TEMPLATES`, `TEMPLATE_CACHE` | See [templates](templates.md). |
| `CACHES` | See [caching](cache.md). |
| `STORAGES` | See [storage](storage.md). |
| `INTERCOMS` | See [Intercom](intercom.md). |
| `EMAIL_*`, `DEFAULT_FROM_EMAIL` | See [mail](mail.md). |
| `OBSERVABILITY_*` | See [observability](observability.md). |
| `TIME_ZONE`, `USE_TZ` | Read by `nitro.utils.datetime`. |
| `LANGUAGE_CODE`, `LANGUAGES` | Read by `nitro.utils.translation`. |

## Allowed hosts

A client chooses its own `Host` header, so a server that answers to any name
will put whatever it was given into anything built from it — a password reset
link, a cached response, an absolute redirect.

```python
ALLOWED_HOSTS = ["example.com", ".internal.example.com"]
```

| Entry | Matches |
|---|---|
| `example.com` | that name exactly |
| `.example.com` | `example.com` and every subdomain of it |
| `*` | any name |
| *(empty list)* | any name |

The check runs in the compiled server, before a request reaches the
application: a refused one is answered `400` without resolving a dependency,
matching a route or reaching a handler. Ports and letter case are ignored, so
`Example.com:8000` matches `example.com`.

An empty list means "not configured" and answers to anything, which is right
while developing. `nitro check` refuses to pass a deployment that left it empty
with `DEBUG` off.

## Observability

Flat rather than nested, because there is exactly one exporter. They are read
from the top level, like every other server option.

```python
OBSERVABILITY_ENABLED = True
OBSERVABILITY_HOST = "localhost"
OBSERVABILITY_PORT = 9464
```

| Setting | Default | Meaning |
|---|---|---|
| `OBSERVABILITY_ENABLED` | `False` | Whether anything listens. Metrics are collected either way. |
| `OBSERVABILITY_HOST` | `"localhost"` | Loopback on purpose. `"0.0.0.0"` publishes internal counters. |
| `OBSERVABILITY_PORT` | `9464` | A port of its own, never the application's. Each worker takes the next one up. |

See [observability](observability.md) for what is measured.

## Reading them yourself

```python
from nitro.settings import ImproperlyConfigured, settings

try:
    url: str = settings.DATABASE_URL
except AttributeError:
    raise ImproperlyConfigured("DATABASE_URL is not set") from None
```

`nitro check` reports the configuration problems that will stop a deployment
working, and exits non-zero when it finds any.
