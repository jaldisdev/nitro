//! The metrics themselves.
//!
//! Every metric is registered into the process-global default registry the
//! moment it is first touched, and whatever is registered at scrape time is
//! what gets rendered. That means instrumentation costs an atomic increment
//! whether or not anyone is scraping, and enabling the exporter later needs no
//! rebuild — only a listener.
//!
//! Names follow one shape: `nitro_<area>_<noun>_total` for counters,
//! `nitro_<area>_<noun>` for gauges and histograms, with lower-case labels.

use std::sync::LazyLock;
use std::time::Duration;

use prometheus::{
    Encoder, HistogramVec, IntCounterVec, IntGauge, IntGaugeVec, TextEncoder, histogram_opts, opts,
};

/// Latency buckets, in seconds.
///
/// Weighted towards the fast end because that is where a server's interesting
/// variation lives: the difference between 1 ms and 10 ms matters, the
/// difference between 5 s and 6 s does not.
const LATENCY_BUCKETS: &[f64] = &[
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
];

/// Registering can only fail on a duplicate name or a malformed descriptor,
/// both of which are programming errors fixed at the call site rather than
/// conditions to handle at runtime.
fn register<C: prometheus::core::Collector + Clone + 'static>(collector: C) -> C {
    if let Err(error) = prometheus::default_registry().register(Box::new(collector.clone())) {
        tracing::error!(%error, "a metric could not be registered and will not be reported");
    }
    collector
}

pub static REQUESTS: LazyLock<IntCounterVec> = LazyLock::new(|| {
    register(
        IntCounterVec::new(
            opts!(
                "nitro_http_requests_total",
                "HTTP requests served, by route, method and status class"
            ),
            &["route", "method", "status"],
        )
        .expect("the request counter descriptor is valid"),
    )
});

pub static REQUEST_DURATION: LazyLock<HistogramVec> = LazyLock::new(|| {
    register(
        HistogramVec::new(
            histogram_opts!(
                "nitro_http_request_duration_seconds",
                "Time from reading a request to handing its response to the transport, by route and method",
                LATENCY_BUCKETS.to_vec()
            ),
            &["route", "method"],
        )
        .expect("the request duration descriptor is valid"),
    )
});

pub static REQUESTS_IN_FLIGHT: LazyLock<IntGauge> = LazyLock::new(|| {
    register(
        IntGauge::with_opts(opts!(
            "nitro_http_requests_in_flight",
            "Requests being handled right now"
        ))
        .expect("the in-flight descriptor is valid"),
    )
});

pub static CONNECTIONS: LazyLock<IntCounterVec> = LazyLock::new(|| {
    register(
        IntCounterVec::new(
            opts!(
                "nitro_connections_total",
                "Connections accepted, by transport"
            ),
            &["transport"],
        )
        .expect("the connection counter descriptor is valid"),
    )
});

pub static CONNECTIONS_ACTIVE: LazyLock<IntGaugeVec> = LazyLock::new(|| {
    register(
        IntGaugeVec::new(
            opts!(
                "nitro_connections_active",
                "Connections open right now, by transport"
            ),
            &["transport"],
        )
        .expect("the active connection descriptor is valid"),
    )
});

pub static SOCKETS: LazyLock<IntCounterVec> = LazyLock::new(|| {
    register(
        IntCounterVec::new(
            opts!(
                "nitro_sockets_total",
                "WebSocket and WebTransport handshakes, by protocol and outcome"
            ),
            &["protocol", "outcome"],
        )
        .expect("the socket counter descriptor is valid"),
    )
});

pub static SOCKETS_ACTIVE: LazyLock<IntGaugeVec> = LazyLock::new(|| {
    register(
        IntGaugeVec::new(
            opts!(
                "nitro_sockets_active",
                "WebSocket connections and WebTransport sessions open right now, by protocol"
            ),
            &["protocol"],
        )
        .expect("the active socket descriptor is valid"),
    )
});

pub static WORKER_STARTED: LazyLock<IntGauge> = LazyLock::new(|| {
    register(
        IntGauge::with_opts(opts!(
            "nitro_worker_start_time_seconds",
            "When this worker began serving, in seconds since the epoch"
        ))
        .expect("the start time descriptor is valid"),
    )
});

pub static WORKER_DRAINING: LazyLock<IntGauge> = LazyLock::new(|| {
    register(
        IntGauge::with_opts(opts!(
            "nitro_worker_draining",
            "1 once this worker has begun shutting down, 0 while it is serving"
        ))
        .expect("the draining descriptor is valid"),
    )
});

/// Which transport a connection arrived over.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Transport {
    Tcp,
    Unix,
    Quic,
}

impl Transport {
    /// Every transport there is, so the series for each can exist before one
    /// has carried anything.
    pub const ALL: [Self; 3] = [Self::Tcp, Self::Unix, Self::Quic];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Tcp => "tcp",
            Self::Unix => "unix",
            Self::Quic => "quic",
        }
    }
}

/// Which of the two long-lived protocols a connection is speaking.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SocketProtocol {
    WebSocket,
    WebTransport,
}

impl SocketProtocol {
    pub const ALL: [Self; 2] = [Self::WebSocket, Self::WebTransport];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::WebSocket => "websocket",
            Self::WebTransport => "webtransport",
        }
    }
}

/// Record a served request.
///
/// `route` is the registered path pattern rather than the path that was asked
/// for. Using the concrete path would give every identifier its own time
/// series — a cardinality explosion that makes the metric useless and the
/// scrape expensive. A request that matched no route is reported as `unmatched`
/// for the same reason.
pub fn record_request(route: Option<&str>, method: &str, status: u16, elapsed: Duration) {
    let route = route.unwrap_or("unmatched");
    let status = status_class(status);

    REQUESTS.with_label_values(&[route, method, status]).inc();
    REQUEST_DURATION
        .with_label_values(&[route, method])
        .observe(elapsed.as_secs_f64());
}

/// The status class as a label value: `2xx`, `4xx` and so on.
///
/// The exact code is deliberately not a label. Class is what alerting rules
/// actually ask about, and it keeps one series per class rather than one per
/// code for every route.
fn status_class(status: u16) -> &'static str {
    match status {
        100..=199 => "1xx",
        200..=299 => "2xx",
        300..=399 => "3xx",
        400..=499 => "4xx",
        500..=599 => "5xx",
        _ => "unknown",
    }
}

pub fn request_started() {
    REQUESTS_IN_FLIGHT.inc();
}

pub fn request_finished() {
    REQUESTS_IN_FLIGHT.dec();
}

pub fn connection_opened(transport: Transport) {
    CONNECTIONS.with_label_values(&[transport.as_str()]).inc();
    CONNECTIONS_ACTIVE
        .with_label_values(&[transport.as_str()])
        .inc();
}

pub fn connection_closed(transport: Transport) {
    CONNECTIONS_ACTIVE
        .with_label_values(&[transport.as_str()])
        .dec();
}

/// Record how a handshake was answered. `outcome` is `accepted` or `refused`.
pub fn socket_handshake(protocol: SocketProtocol, accepted: bool) {
    let outcome = if accepted { "accepted" } else { "refused" };
    SOCKETS
        .with_label_values(&[protocol.as_str(), outcome])
        .inc();
    if accepted {
        SOCKETS_ACTIVE.with_label_values(&[protocol.as_str()]).inc();
    }
}

pub fn socket_closed(protocol: SocketProtocol) {
    SOCKETS_ACTIVE.with_label_values(&[protocol.as_str()]).dec();
}

/// Note that this worker has started serving.
pub fn worker_started() {
    let epoch_seconds = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs() as i64)
        .unwrap_or(0);
    WORKER_STARTED.set(epoch_seconds);
    WORKER_DRAINING.set(0);
    declare_known_series();
}

/// Create, at zero, every series whose labels are known before any traffic.
///
/// A labelled metric has no series until something observes one, and a metric
/// with no series is absent from a scrape entirely -- no samples, and no `HELP`
/// or `TYPE` either. A counter that appears only once it is non-zero is worse
/// than useless to query: `rate()` over it returns nothing rather than zero,
/// and an alert on its absence fires at exactly the wrong moment. The label
/// sets here are small and known, so the series are made up front.
///
/// Requests are deliberately not among them: their labels include the route,
/// and inventing a series for every route at startup is the unbounded version
/// of this idea.
fn declare_known_series() {
    for transport in Transport::ALL {
        CONNECTIONS
            .with_label_values(&[transport.as_str()])
            .inc_by(0);
        CONNECTIONS_ACTIVE
            .with_label_values(&[transport.as_str()])
            .set(0);
    }

    for protocol in SocketProtocol::ALL {
        for outcome in ["accepted", "refused"] {
            SOCKETS
                .with_label_values(&[protocol.as_str(), outcome])
                .inc_by(0);
        }
        SOCKETS_ACTIVE
            .with_label_values(&[protocol.as_str()])
            .set(0);
    }
}

/// Note that this worker has begun shutting down.
pub fn worker_draining() {
    WORKER_DRAINING.set(1);
}

/// Render everything registered in this process as Prometheus text exposition
/// format.
pub fn render() -> String {
    let families = prometheus::gather();
    let mut buffer = Vec::new();

    if let Err(error) = TextEncoder::new().encode(&families, &mut buffer) {
        tracing::error!(%error, "metrics could not be encoded");
        return String::new();
    }

    String::from_utf8(buffer).unwrap_or_else(|error| {
        tracing::error!(%error, "the encoded metrics were not valid text");
        String::new()
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_worker_that_has_served_nothing_still_exposes_what_it_can() {
        // A scrape a moment after startup has to carry every metric whose
        // labels are known, or a counter appears only once it is non-zero and
        // nothing can be queried against it in the meantime.
        worker_started();

        let rendered = render();
        for name in [
            "nitro_connections_total",
            "nitro_connections_active",
            "nitro_sockets_total",
            "nitro_sockets_active",
        ] {
            assert!(
                rendered.contains(&format!("# HELP {name}")),
                "{name} is missing"
            );
        }

        // The series must exist; what it holds is another test's business, as
        // the registry is shared by every test in this process.
        for transport in Transport::ALL {
            assert!(
                rendered.contains(&format!(
                    "nitro_connections_total{{transport=\"{}\"}}",
                    transport.as_str()
                )),
                "{} has no series",
                transport.as_str()
            );
        }
    }

    #[test]
    fn status_codes_are_reported_by_class() {
        assert_eq!(status_class(200), "2xx");
        assert_eq!(status_class(204), "2xx");
        assert_eq!(status_class(301), "3xx");
        assert_eq!(status_class(404), "4xx");
        assert_eq!(status_class(500), "5xx");
        assert_eq!(status_class(101), "1xx");
        assert_eq!(status_class(999), "unknown");
    }

    #[test]
    fn a_request_is_counted_and_timed() {
        record_request(
            Some("/users/<int:id>"),
            "GET",
            200,
            Duration::from_millis(5),
        );

        let rendered = render();
        assert!(rendered.contains("nitro_http_requests_total"));
        assert!(rendered.contains(r#"route="/users/<int:id>""#));
        assert!(rendered.contains(r#"status="2xx""#));
        assert!(rendered.contains("nitro_http_request_duration_seconds"));
    }

    #[test]
    fn an_unmatched_request_does_not_become_its_own_series() {
        record_request(None, "GET", 404, Duration::from_millis(1));

        let rendered = render();
        assert!(
            rendered.contains(r#"route="unmatched""#),
            "a path that matched no route must not be labelled with the path itself"
        );
    }

    #[test]
    fn in_flight_rises_and_falls() {
        let before = REQUESTS_IN_FLIGHT.get();
        request_started();
        assert_eq!(REQUESTS_IN_FLIGHT.get(), before + 1);
        request_finished();
        assert_eq!(REQUESTS_IN_FLIGHT.get(), before);
    }

    #[test]
    fn connections_are_counted_per_transport() {
        connection_opened(Transport::Tcp);
        let active = CONNECTIONS_ACTIVE.with_label_values(&["tcp"]).get();
        connection_closed(Transport::Tcp);

        assert_eq!(
            CONNECTIONS_ACTIVE.with_label_values(&["tcp"]).get(),
            active - 1
        );
        assert!(render().contains("nitro_connections_total"));
    }

    #[test]
    fn a_refused_handshake_is_counted_but_not_held_open() {
        let before = SOCKETS_ACTIVE.with_label_values(&["websocket"]).get();

        socket_handshake(SocketProtocol::WebSocket, false);
        assert_eq!(
            SOCKETS_ACTIVE.with_label_values(&["websocket"]).get(),
            before,
            "a refused handshake never became an open connection"
        );

        socket_handshake(SocketProtocol::WebSocket, true);
        assert_eq!(
            SOCKETS_ACTIVE.with_label_values(&["websocket"]).get(),
            before + 1
        );
        socket_closed(SocketProtocol::WebSocket);
        assert_eq!(
            SOCKETS_ACTIVE.with_label_values(&["websocket"]).get(),
            before
        );
    }

    #[test]
    fn worker_lifecycle_is_reported() {
        worker_started();
        assert!(WORKER_STARTED.get() > 0);
        assert_eq!(WORKER_DRAINING.get(), 0);

        worker_draining();
        assert_eq!(WORKER_DRAINING.get(), 1);
        worker_started();
    }

    #[test]
    fn transports_and_protocols_have_stable_label_values() {
        assert_eq!(Transport::Tcp.as_str(), "tcp");
        assert_eq!(Transport::Unix.as_str(), "unix");
        assert_eq!(Transport::Quic.as_str(), "quic");
        assert_eq!(SocketProtocol::WebSocket.as_str(), "websocket");
        assert_eq!(SocketProtocol::WebTransport.as_str(), "webtransport");
    }

    #[test]
    fn rendering_produces_exposition_format() {
        record_request(Some("/"), "GET", 200, Duration::from_millis(1));
        let rendered = render();

        assert!(rendered.contains("# HELP nitro_http_requests_total"));
        assert!(rendered.contains("# TYPE nitro_http_requests_total counter"));
    }
}
