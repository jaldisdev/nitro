//! Prometheus metrics for the Nitro server.
//!
//! Split out from the transport so that other parts of the system can report
//! metrics without depending on the server's accept loops and lifecycle.
//!
//! Metrics are always collected — an increment on a counter costs an atomic
//! operation whether or not anyone is looking. What the configuration decides
//! is only whether a listener exists for a scraper to read from.

pub mod exporter;
pub mod metrics;

pub use exporter::{
    BoundExporter, DEFAULT_HOST, DEFAULT_PORT, ExporterConfig, ExporterError, METRICS_PATH,
};
pub use metrics::{
    SocketProtocol, Transport, connection_closed, connection_opened, record_request, render,
    request_finished, request_started, socket_closed, socket_handshake, worker_draining,
    worker_started,
};
