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

`SERVER` holds the bundled server's own options. Anything left out keeps its
default.

```python
SERVER = {
    "HOST": "0.0.0.0",
    "PORT": 8000,
    "WORKERS": 4,
    "HTTP": "auto",
    "TLS_CERT": "/etc/tls/site.pem",
    "TLS_KEY": "/etc/tls/site.key",
    "ACCESS_LOG": True,
}
```

| Key | Default | Meaning |
|---|---|---|
| `HOST`, `PORT` | `localhost`, `8000` | Where to listen. Every address the host resolves to gets a socket. |
| `UDS` | `None` | A Unix socket instead of a port. |
| `HTTP` | `"auto"` | Highest version to negotiate: `"1"`, `"2"`, `"3"`, or `"auto"` for the highest available. |
| `WEBSOCKETS`, `WEBTRANSPORT` | `True` | Whether upgrades are honoured. WebTransport needs HTTP/3 and switches off without it. |
| `TLS_CERT`, `TLS_KEY` | `None` | Required whenever HTTP/3 is active. |
| `TLS_CA`, `TLS_CLIENT_AUTH` | `None`, `"none"` | Client certificates: `"none"`, `"optional"` or `"required"`. |
| `TLS_TCP` | `True` | Terminate TLS on the TCP socket. Off when a proxy already does. |
| `TLS_RELOAD_INTERVAL` | `10.0` | Seconds between certificate checks. `0` disables reloading. |
| `WORKERS`, `RUNTIME_THREADS` | `1`, `1` | Processes, and threads within each one's runtime. |
| `BACKLOG` | `1024` | Kernel accept queue depth. |
| `MAX_CONCURRENT_CONNECTIONS` | `None` | Per-worker cap. Beyond it, connections wait in the backlog. |
| `DATAGRAM_QUEUE_CAPACITY` | `64` | Datagrams held per session before the oldest is dropped. |
| `STREAM_QUEUE_CAPACITY` | `16` | How far a streaming response may run ahead of its client. |
| `ALT_SVC` | `"auto"` | Advertising HTTP/3: `"auto"`, `"off"`, or a verbatim header value. |
| `DRAIN_TIMEOUT` | `30.0` | Seconds in-flight work gets once shutdown starts. |
| `SERVER_HEADER` | `"nitro"` | `None` omits the header. |
| `LOG_LEVEL`, `LOG_DESTINATION`, `LOG_FORMAT` | `"info"`, `"stderr"`, `"pretty"` | The server log. A destination other than `stdout`/`stderr` is a file path. |
| `ACCESS_LOG`, `ACCESS_LOG_DESTINATION`, `ACCESS_LOG_FORMAT` | `False`, `"stdout"`, `"combined"` | The access log, configured separately. |

### Where a value comes from

Three places, in increasing order of precedence:

1. the `SERVER` setting;
2. keyword arguments to `Nitro(...)`;
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
| `SECRET_KEY` | Signing. Required outside debug. |
| `ALLOWED_HOSTS` | Host names this site answers for. `"*"` matches anything; a leading dot matches subdomains. |
| `MIDDLEWARE` | Import paths, outermost first. See [middleware](middleware.md). |
| `COMMAND_MODULES` | Packages searched for extra CLI commands. |
| `ROUTES` | The module holding the route table, which defines `patterns`. See [routing](routing.md). |
| `TEMPLATES`, `TEMPLATE_CACHE` | See [templates](templates.md). |
| `CACHES` | See [caching](cache.md). |
| `STORAGES` | See [storage](storage.md). |
| `INTERCOMS` | See [Intercom](intercom.md). |
| `EMAIL_*`, `DEFAULT_FROM_EMAIL` | See [mail](mail.md). |
| `OBSERVABILITY_*` | See [observability](observability.md). |
| `TIME_ZONE`, `USE_TZ`, `LANGUAGE_CODE`, `LANGUAGES` | Localisation. |

## Observability

Flat rather than nested, because there is exactly one exporter. They are read
from the top level; putting them inside `SERVER` is an error.

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
