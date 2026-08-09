# Observability

The server keeps Prometheus counters, gauges and histograms for everything it
serves. They are collected whether or not anyone reads them; turning
observability on only starts a listener that exposes them.

```python
OBSERVABILITY_ENABLED = True
OBSERVABILITY_HOST = "localhost"
OBSERVABILITY_PORT = 9464
```

```sh
curl http://localhost:9464/metrics
```

## A port of its own

The endpoint does not live in your route table and does not share the
application's port. That is deliberate:

- it can be firewalled and scraped separately from public traffic;
- it does not pass through your middleware, authentication or host checking,
  none of which a scraper should have to satisfy;
- adding a route cannot accidentally shadow it, and it cannot accidentally
  shadow one of yours.

`OBSERVABILITY_HOST` defaults to `localhost`, so the endpoint is reachable from
a scraper running alongside the server and from nowhere else. Setting it to
`0.0.0.0` publishes your request rates, latencies and error counts to anyone
who can reach the port; do that only behind a network you control.

These are flat settings rather than a dictionary because there is exactly one
exporter to configure. They are read from the top level, not from `SERVER` —
putting them there is an error rather than a silent no-op.

## One endpoint per worker

Workers are separate processes with separate counters, so they cannot share a
port: a scrape would be answered by whichever worker happened to accept it, and
the numbers would jump between workers from one scrape to the next.

Each worker listens on `OBSERVABILITY_PORT` plus its index instead. Four workers
starting from `9464` means `9464`, `9465`, `9466` and `9467`. Point the scraper
at all of them and sum:

```yaml
scrape_configs:
  - job_name: nitro
    static_configs:
      - targets:
          - "localhost:9464"
          - "localhost:9465"
          - "localhost:9466"
          - "localhost:9467"
```

The whole range is bound in the parent before any worker is forked, like the
application's own sockets, so a clash is one clear error at startup rather than
one per worker after the process appears to have started. Leave room for the
range when choosing a port.

## What is measured

| Metric | Type | Labels |
|---|---|---|
| `nitro_http_requests_total` | counter | `route`, `method`, `status` |
| `nitro_http_request_duration_seconds` | histogram | `route`, `method` |
| `nitro_http_requests_in_flight` | gauge | — |
| `nitro_connections_total` | counter | `transport` |
| `nitro_connections_active` | gauge | `transport` |
| `nitro_sockets_total` | counter | `protocol`, `outcome` |
| `nitro_sockets_active` | gauge | `protocol` |
| `nitro_worker_start_time_seconds` | gauge | — |
| `nitro_worker_draining` | gauge | — |

`transport` is `tcp`, `unix` or `quic`. `protocol` is `websocket` or
`webtransport`, and `outcome` is `accepted` or `refused`, so a handler that
rejects handshakes is visible as such rather than as an absence.

WebSocket connections and WebTransport sessions get their own counters because
they are long-lived: a request histogram says nothing about a socket that has
been open for an hour, and `nitro_sockets_active` is what tells you whether one
worker is holding connections the others are not.

`nitro_worker_draining` goes to `1` the moment a worker begins shutting down,
which is what distinguishes a worker that is finishing in-flight work from one
that has stopped answering.

## Labels and cardinality

`route` is the pattern a request matched, not the path it asked for:
`/users/<int:user_id>`, never `/users/8814`. A concrete path would give every
identifier a time series of its own, which makes the metric unreadable and the
scrape expensive. A request that matched no route is reported as `unmatched`
for the same reason.

`status` is the class — `2xx`, `4xx`, `5xx` — rather than the exact code. Class
is what alerting rules ask about, and it keeps one series per class instead of
one per code per route.

## Scraping it

With one worker there is one target:

```yaml
scrape_configs:
  - job_name: nitro
    static_configs:
      - targets: ["localhost:9464"]
```

The endpoint answers `/metrics` and nothing else; every other path is a 404. The
response is Prometheus text exposition format, version 0.0.4.

## What it does not do

There is no OpenTelemetry export, no tracing, and no UI. The endpoint is a
scrape target; reading and alerting on it belongs to Prometheus.
