# Running in production

The server is part of the application, so deploying means running `nitro`.
There is no ASGI server to choose and no worker class to configure.

```sh
nitro app:app --workers 4 --host 0.0.0.0 --port 8000
```

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

## Shutting down

On `SIGINT` or `SIGTERM` the parent asks every worker to stop and each one runs
the same chain:

1. accept loops return, so no new connection is taken on;
2. handlers waiting on a disconnect are released, which is how a long-lived
   response learns to wind down;
3. open connections are asked to finish their current exchange and close;
4. background tasks are awaited.

Steps 3 and 4 share one deadline, `DRAIN_TIMEOUT`, defaulting to thirty
seconds. Whatever has not finished by then is abandoned — a shutdown one stuck
handler can delay indefinitely is not a shutdown. The parent then waits that
long plus a grace period before killing anything left.

Set `DRAIN_TIMEOUT` to something a little under your orchestrator's own
termination grace period, so the server finishes on its own terms rather than
being killed mid-drain.

## TLS

```python
SERVER = {
    "TLS_CERT": "/etc/tls/site.pem",
    "TLS_KEY": "/etc/tls/site.key",
}
```

The certificate file is watched and reloaded when it changes, so renewal does
not need a restart. Established connections keep the certificate they
negotiated; new handshakes use the new one. A replacement that cannot be read —
a renewal caught mid-write, say — is logged and ignored, and the previous
certificate stays in use.

Set `TLS_RELOAD_INTERVAL` to `0` to switch reloading off.

### Behind a proxy that terminates TLS

```python
SERVER = {"TLS_TCP": False, "HTTP": "2"}
```

`TLS_TCP` turns off TLS on the TCP socket only. QUIC always carries its own,
because the protocol requires it — which is why HTTP/3 needs a certificate even
when a proxy is terminating TLS for HTTP/1.1 and HTTP/2.

## HTTP/3

HTTP/3 needs a UDP socket alongside the TCP one, on the same port, and a
certificate. Both are bound when `HTTP` is `"auto"` or `"3"`.

Clients discover it through the `Alt-Svc` header, which is added automatically
to responses served over TCP. If your public port differs from the bound port —
behind a load balancer, usually — set the header yourself:

```python
SERVER = {"ALT_SVC": 'h3=":443"; ma=86400'}
```

Set `ALT_SVC` to `"off"` where a proxy advertises its own endpoint.

## Backpressure

`MAX_CONCURRENT_CONNECTIONS` caps how many connections a worker serves at once.
Beyond it, connections wait in the kernel's backlog rather than being accepted
and left unserved — a waiting client sees a slow connect, which is honest,
instead of a connection that opens and then does nothing.

`STREAM_QUEUE_CAPACITY` sets how far ahead a streaming response may run. Sending
waits once that many chunks are queued, so a producer faster than its client is
slowed to the client's pace rather than filling memory.

## Logging

The server log and the access log are configured separately and can go to
different places in different formats.

```python
SERVER = {
    "LOG_LEVEL": "info",
    "LOG_FORMAT": "json",
    "ACCESS_LOG": True,
    "ACCESS_LOG_DESTINATION": "/var/log/nitro/access.log",
    "ACCESS_LOG_FORMAT": "combined",
}
```

`RUST_LOG` overrides `LOG_LEVEL` when it is set, which is useful for turning up
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
