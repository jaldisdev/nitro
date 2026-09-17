# Logging

A Nitro process keeps two logs, configured separately.

| Log | Written by | Configured by |
|---|---|---|
| The server log | The compiled server: startup, connections, TLS, shutdown | `SERVER_LOG_*`, and `RUST_LOG`. See [deployment](deployment.md#logging). |
| The Python log | The framework's `nitro.*` loggers and your application's own | `LOGGING` |

This page is about the second. Neither setting reaches the other log:
`SERVER_LOG_LEVEL = "debug"` does not show the framework's debug records, and a
handler in `LOGGING` never sees a line from the server.

## The defaults

With `LOGGING` left empty, the `nitro` logger writes `INFO` and above to stderr:

```text
2026-09-17T10:12:03.123456Z  WARN nitro.mail.backends.smtp: the SMTP connection did not close cleanly: timed out
2026-09-17T10:12:04.654321Z ERROR nitro: handler for GET /boom failed
Traceback (most recent call last):
  ...
RuntimeError: deliberate
```

The layout is the server's default text layout, so the two logs read alike and
sort together when they share a terminal. Only the layout is shared: setting
`SERVER_LOG_FORMAT = "json"` leaves these lines as they are.

The root logger is not touched. Your application's own loggers behave as
Python's do until you configure them, which means only warnings and errors are
printed, without a timestamp.

## When it is applied

The command line applies `LOGGING` before it runs anything, whether that is
serving, `check`, `shell` or a command of your own, and `app.serve()` applies
it before starting the server. Workers inherit it when they are forked.

Nothing else does. Importing `nitro` or building a `Nitro()` leaves logging as
it was, so an application run inside another program, or inside a test, does
not take over that program's logging.

## Configuring it

`LOGGING` is a [`logging.config.dictConfig`][dictconfig] mapping. Its
`formatters`, `filters`, `handlers` and `loggers` are merged by name over
Nitro's defaults, so a setting only has to say what it changes:

```python
LOGGING = {
    "loggers": {
        "nitro": {"handlers": ["nitro"], "level": "DEBUG"},
    },
}
```

Merging stops at the entry: one of yours replaces Nitro's entry of the same
name whole, which is why the logger above names its handler again.

The defaults are named `nitro` in every section: a formatter, a handler and a
logger. An entry of your own under one of those names replaces Nitro's, so the
framework's log can be moved without restating the rest:

```python
LOGGING = {
    "handlers": {
        "nitro": {
            "class": "logging.FileHandler",
            "filename": "/var/log/myproject/nitro.log",
            "formatter": "nitro",
        },
    },
}
```

Your application's loggers are configured the same way:

```python
LOGGING = {
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "nitro"},
    },
    "loggers": {
        "myproject": {"handlers": ["console"], "level": "INFO"},
    },
}
```

`disable_existing_loggers` defaults to `False` here, where `dictConfig` would
default it to `True`. Loggers are created when their modules are imported,
which is before `LOGGING` is applied, and switching them off without saying so
is rarely what a setting meant.

### Duplicate lines

The `nitro` logger passes its records on to the root logger. That is what lets
a handler on the root see framework errors, but it also means a root handler
that writes to stderr prints every framework line a second time. Either send
the `nitro` logger to nothing of its own:

```python
LOGGING = {
    "root": {"handlers": ["console"], "level": "INFO"},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "nitro"},
    },
    "loggers": {
        "nitro": {"handlers": [], "level": "INFO"},
    },
}
```

or keep its handler and set `"propagate": False` on it, if the root's handlers
should not see framework records at all.

### JSON

The standard library has no JSON formatter, and Nitro does not add one. Name
any `logging.Formatter` subclass with `()`:

```python
LOGGING = {
    "formatters": {
        "json": {"()": "myproject.logging.JSONFormatter"},
    },
    "handlers": {
        "nitro": {"class": "logging.StreamHandler", "formatter": "json"},
    },
}
```

## A setting that cannot be applied

A `LOGGING` that `dictConfig` rejects — a handler class that does not import, a
file that cannot be opened, a section that is not a mapping — does not stop the
process. The defaults are applied in its place, and a warning says why:

```text
2026-09-17T10:12:03.123456Z  WARN nitro: ignoring the LOGGING setting: Unable to configure handler 'sentry': Cannot resolve 'sentry_sdk.integrations.logging.EventHandler': No module named 'sentry_sdk'
```

Starting with the defaults is better than not starting, but it may mean an
error tracker is not connected. `nitro check` reports the same problem and
exits non-zero, so a release gated on it will not ship one.

## Sending records elsewhere

Any `logging.Handler` works: an error tracker's, `logging.handlers.SysLogHandler`,
or one of your own.

```python
LOGGING = {
    "handlers": {
        "errors": {"class": "myproject.logging.ErrorTrackerHandler", "level": "ERROR"},
    },
    "loggers": {
        "nitro": {"handlers": ["nitro", "errors"], "level": "INFO"},
    },
}
```

### Why there is no email handler

Nitro does not ship a handler that emails errors to administrators.

- A handler's `emit` is synchronous, and Nitro's [mail](mail.md) backends are
  coroutines. Blocking on a send stalls the event loop during exactly the
  incident being reported. Sending from a background task instead means a
  failed send goes unnoticed, and a worker shutting down can cut it off.
- One email per error means one email per failing request during an outage,
  which is enough to get an SMTP or SES account throttled.
- Useful error reports carry tracebacks and request details, and those hold
  credentials, cookies and personal data that would need filtering before they
  leave the process.

To be told that errors are happening, alert on
`nitro_http_requests_total{status="5xx"}` from the
[metrics endpoint](observability.md). To see what the errors were, use an error
tracker's handler. If email is still what you want, a handler of your own can
send it, once it answers the three points above.

[dictconfig]: https://docs.python.org/3/library/logging.config.html#logging-config-dictschema
