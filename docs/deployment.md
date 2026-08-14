# Running in production

The server is part of the application, so deploying means running `nitro`.
There is no ASGI server to choose and no worker class to configure.

```sh
nitro app:app --workers 4 --host 0.0.0.0 --port 8000
```

## Serving from the application itself

`app.serve()` starts the same server from Python, which makes the application
file its own entry point:

```python
# app.py
app = Nitro()

if __name__ == "__main__":
    app.serve()
```

```dockerfile
CMD ["python", "app.py"]
```

Keyword arguments override the server options exactly as the command line flags
do — `app.serve(host="0.0.0.0", workers=4)`.

This is for a container or a process supervisor, where one executable file is
worth more than a command line to get wrong. For development the command line
is the easier of the two, since it takes flags without editing anything.

Both go through the same code, so an application serves identically either way.
A settings module still has to be named before the application is imported; a
file that sets `NITRO_SETTINGS_MODULE` itself before importing Nitro covers
that, and is the usual shape for an entry point.

## Workers

Each worker is a separate process, forked from the parent after the sockets are
bound. A worker has its own runtime, its own event loop and its own signal
handling, so nothing about one worker's shutdown involves the others.

Forking rather than starting fresh interpreters is deliberate. Measured on
CPython 3.14, a fork costs about a millisecond against about forty-five for a
new interpreter, and the forked worker shares the already-imported application
through copy-on-write instead of importing it again. Threads were measured at
1.34× on four cores for CPU-bound Python against 4.68× for processes, so worker
threads are not offered: on a build with the global interpreter lock they buy
almost nothing.

Two consequences worth knowing:

- **Anything opened before the fork is shared.** A database connection or
  cache client created at import time ends up used by every worker at once.
  Open connections in `__startup__` or on first use inside the worker. The
  bundled cache, storage and Intercom registries do this for you, and are reset
  in each worker as it starts.
- **Windows gets one worker.** There is no fork there; `--workers` above one is
  refused rather than silently ignored.

The parent supervises. A worker that exits unexpectedly is replaced after a
short delay, so a crash does not quietly reduce capacity.

## Runtime threads

Each worker's runtime gets two threads by default. They are not for running
handlers — handlers run on the worker's event loop, one thread, as they do in
any asyncio server. They read sockets, parse requests and write responses, none
of which needs the interpreter, so a second one overlaps that work with the
Python the loop is running.

```python
SERVER_RUNTIME_THREADS = 2
```

Two is worth about a quarter more throughput than one and keeps its advantage as
connections pile up, where one levels off and then declines. Four measured no
better than two: past that the loop, not the sockets, is what the server is
waiting for. Raise it only if a profile says the runtime threads are busy and
the loop is not.

## Shutting down

On `SIGINT` or `SIGTERM` the parent asks every worker to stop and each one runs
the same chain:

1. accept loops return, so no new connection is taken on;
2. handlers waiting on a disconnect are released, which is how a long-lived
   response learns to wind down;
3. open connections are asked to finish their current exchange and close;
4. background tasks are awaited.

Steps 3 and 4 share one deadline, `SERVER_DRAIN_TIMEOUT`, defaulting to thirty
seconds. Whatever has not finished by then is abandoned — a shutdown one stuck
handler can delay indefinitely is not a shutdown. The parent then waits that
long plus a grace period before killing anything left.

Set `SERVER_DRAIN_TIMEOUT` to something a little under your orchestrator's own
termination grace period, so the server finishes on its own terms rather than
being killed mid-drain.

## TLS

```python
SERVER_TLS_CERT = "/etc/tls/site.pem"
SERVER_TLS_KEY = "/etc/tls/site.key"
```

The certificate file is watched and reloaded when it changes, so renewal does
not need a restart. Established connections keep the certificate they
negotiated; new handshakes use the new one. A replacement that cannot be read —
a renewal caught mid-write, say — is logged and ignored, and the previous
certificate stays in use.

Set `SERVER_TLS_RELOAD_INTERVAL` to `0` to switch reloading off.

### Behind a proxy that terminates TLS

```python
SERVER_TLS_TCP = False
SERVER_HTTP = "2"
```

`SERVER_TLS_TCP` turns off TLS on the TCP socket only. QUIC always carries its own,
because the protocol requires it — which is why HTTP/3 needs a certificate even
when a proxy is terminating TLS for HTTP/1.1 and HTTP/2.

## HTTP/3

HTTP/3 needs a UDP socket alongside the TCP one, on the same port, and a
certificate. Both are bound when `SERVER_HTTP` is `"auto"` or `"3"`.

Clients discover it through the `Alt-Svc` header, which is added automatically
to responses served over TCP. If your public port differs from the bound port —
behind a load balancer, usually — set the header yourself:

```python
SERVER_ALT_SVC = 'h3=":443"; ma=86400'
```

Set `SERVER_ALT_SVC` to `"off"` where a proxy advertises its own endpoint.

## Backpressure

`SERVER_MAX_CONCURRENT_CONNECTIONS` caps how many connections a worker serves at once.
Beyond it, connections wait in the kernel's backlog rather than being accepted
and left unserved — a waiting client sees a slow connect, which is honest,
instead of a connection that opens and then does nothing.

`SERVER_STREAM_QUEUE_CAPACITY` sets how far ahead a streaming response may run. Sending
waits once that many chunks are queued, so a producer faster than its client is
slowed to the client's pace rather than filling memory.

## Logging

The server log and the access log are configured separately and can go to
different places in different formats.

```python
SERVER_LOG_LEVEL = "info"
SERVER_LOG_FORMAT = "json"
SERVER_ACCESS_LOG = True
SERVER_ACCESS_LOG_DESTINATION = "/var/log/nitro/access.log"
SERVER_ACCESS_LOG_FORMAT = "combined"
```

`RUST_LOG` overrides `SERVER_LOG_LEVEL` when it is set, which is useful for turning up
detail on a running deployment without changing configuration.

## Metrics

```python
OBSERVABILITY_ENABLED = True
OBSERVABILITY_PORT = 9464
```

The exporter binds on loopback, before the workers are forked, on a port of its
own — one per worker, counting up from `OBSERVABILITY_PORT`, since separate
processes keep separate counters. Point a scraper at every worker's
`/metrics` and reach them over the network the same way you would reach any
other internal endpoint: through the host, not by binding the exporter to every
interface. See [observability](observability.md).

## Checking a deployment

```sh
nitro check
```

Reports configuration that will not work — HTTP/3 without a certificate, an
empty `SECRET_KEY` or `ALLOWED_HOSTS` outside debug — and exits non-zero when it
finds any, so it can gate a release.
