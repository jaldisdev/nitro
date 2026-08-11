//
// This source file is part of the Nitro open source project.
//
// Copyright (c) 2026 Jaldis B.V.
//
// Licensed under the MIT OR Apache-2.0 license (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://opensource.org/licenses/MIT
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//

//! The listener a scraper reads from.
//!
//! It binds a port of its own rather than adding a route to the application.
//! Two reasons: the endpoint can then be firewalled and scraped independently
//! of public traffic, and it stays out of the application's route table, where
//! it would otherwise pass through whatever middleware, authentication and
//! rewriting the application has — none of which a scraper should have to
//! satisfy.
//!
//! It binds to the loopback address by default for the same reason. Internal
//! counters are not something to publish by accident.

use std::io;
use std::net::{SocketAddr, ToSocketAddrs};
use std::time::Duration;

use bytes::Bytes;
use http_body_util::Full;
use hyper::service::service_fn;
use hyper::{Request, Response, StatusCode};
use hyper_util::rt::{TokioExecutor, TokioIo};
use socket2::{Domain, Protocol, Socket, Type};
use tokio::net::TcpListener;

use crate::metrics;

/// Prometheus' text exposition content type, version 0.0.4.
const EXPOSITION_TYPE: &str = "text/plain; version=0.0.4; charset=utf-8";

/// The path a scraper reads.
pub const METRICS_PATH: &str = "/metrics";

/// Where the exporter listens when nothing says otherwise.
///
/// Loopback, so metrics are reachable from a scraper running alongside the
/// server but not from the network.
pub const DEFAULT_HOST: &str = "localhost";

/// The registered default for a Prometheus exporter endpoint. Distinct from
/// the application's own port by design.
pub const DEFAULT_PORT: u16 = 9464;

#[derive(Debug, thiserror::Error)]
pub enum ExporterError {
    #[error("'{host}' did not resolve to any address")]
    Unresolvable { host: String },
    #[error("binding the metrics endpoint to {address}: {source}")]
    Bind {
        address: String,
        #[source]
        source: io::Error,
    },
    #[error("{workers} workers need {workers} consecutive ports, which do not fit")]
    TooManyWorkers { workers: usize },
}

/// Where and whether to expose metrics.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExporterConfig {
    /// Off unless an operator turns it on. The metrics themselves are always
    /// collected; this only decides whether anything listens.
    pub enabled: bool,
    pub host: String,
    pub port: u16,
}

impl Default for ExporterConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            host: DEFAULT_HOST.to_owned(),
            port: DEFAULT_PORT,
        }
    }
}

/// A bound metrics listener, not yet serving.
///
/// Binding happens before workers are forked, exactly as the application's own
/// sockets do, so a port clash is one clear error at startup.
#[derive(Debug, Default)]
pub struct BoundExporter {
    listeners: Vec<std::net::TcpListener>,
}

impl BoundExporter {
    pub fn addresses(&self) -> Vec<SocketAddr> {
        self.listeners
            .iter()
            .filter_map(|listener| listener.local_addr().ok())
            .collect()
    }

    pub fn is_empty(&self) -> bool {
        self.listeners.is_empty()
    }

    /// Duplicate the descriptors so a forked worker owns its own.
    pub fn duplicate(&self) -> io::Result<Self> {
        Ok(Self {
            listeners: self
                .listeners
                .iter()
                .map(|listener| listener.try_clone())
                .collect::<io::Result<Vec<_>>>()?,
        })
    }
}

/// Bind one exporter per worker, or nothing at all when it is switched off.
///
/// Workers are separate processes with separate counters, so they cannot share
/// a port: a scrape would be answered by whichever worker happened to accept
/// it, and the numbers would jump between workers from one scrape to the next.
/// Each worker gets `port + its index` instead, and a scraper is pointed at all
/// of them and sums what it finds.
///
/// Binding happens here, in the parent, so a clash is one error at startup
/// rather than one per worker after the process appears to have started.
pub fn bind_workers(
    config: &ExporterConfig,
    workers: usize,
) -> Result<Vec<BoundExporter>, ExporterError> {
    if !config.enabled {
        return Ok(Vec::new());
    }

    let mut bound = Vec::with_capacity(workers.max(1));
    for index in 0..workers.max(1) {
        let offset = u16::try_from(index).map_err(|_| ExporterError::TooManyWorkers { workers })?;
        let port = if config.port == 0 {
            // A kernel-chosen port cannot be offset; each worker simply gets
            // one of its own.
            0
        } else {
            config
                .port
                .checked_add(offset)
                .ok_or(ExporterError::TooManyWorkers { workers })?
        };
        bound.push(bind(&ExporterConfig {
            port,
            ..config.clone()
        })?);
    }

    Ok(bound)
}

/// Bind a single exporter, or nothing at all when it is switched off.
pub fn bind(config: &ExporterConfig) -> Result<BoundExporter, ExporterError> {
    if !config.enabled {
        return Ok(BoundExporter {
            listeners: Vec::new(),
        });
    }

    let target = format!("{}:{}", config.host, config.port);
    let addresses: Vec<SocketAddr> = target
        .to_socket_addrs()
        .map_err(|source| ExporterError::Bind {
            address: target.clone(),
            source,
        })?
        .collect();

    if addresses.is_empty() {
        return Err(ExporterError::Unresolvable {
            host: config.host.clone(),
        });
    }

    // As with the application's sockets, a name that resolves to several
    // addresses is served on one port rather than a different ephemeral port
    // per address.
    let mut listeners = Vec::with_capacity(addresses.len());
    let mut chosen = config.port;

    for mut address in addresses {
        if chosen != 0 {
            address.set_port(chosen);
        }
        let listener = bind_one(address)?;
        if chosen == 0 {
            chosen = listener.local_addr().map(|bound| bound.port()).unwrap_or(0);
        }
        listeners.push(listener);
    }

    Ok(BoundExporter { listeners })
}

fn bind_one(address: SocketAddr) -> Result<std::net::TcpListener, ExporterError> {
    let describe = |source: io::Error| ExporterError::Bind {
        address: address.to_string(),
        source,
    };

    let domain = if address.is_ipv4() {
        Domain::IPV4
    } else {
        Domain::IPV6
    };
    let socket = Socket::new(domain, Type::STREAM, Some(Protocol::TCP)).map_err(describe)?;
    socket.set_reuse_address(true).map_err(describe)?;
    socket.bind(&address.into()).map_err(describe)?;
    socket.listen(64).map_err(describe)?;
    socket.set_nonblocking(true).map_err(describe)?;

    tracing::info!(%address, path = METRICS_PATH, "serving metrics");
    Ok(socket.into())
}

/// Serve metrics until `shutdown` resolves.
///
/// Errors serving one scrape are logged rather than propagated: a scraper that
/// hangs up mid-request must not be able to stop the exporter, let alone the
/// server it reports on.
pub async fn serve<S, F>(exporter: BoundExporter, shutdown: S)
where
    S: Fn() -> F + Clone + Send + 'static,
    F: Future<Output = ()> + Send,
{
    let mut loops = Vec::new();

    for listener in exporter.listeners {
        if let Err(error) = listener.set_nonblocking(true) {
            tracing::error!(%error, "the metrics listener could not be prepared");
            continue;
        }
        let listener = match TcpListener::from_std(listener) {
            Ok(listener) => listener,
            Err(error) => {
                tracing::error!(%error, "the metrics listener could not be adopted");
                continue;
            }
        };
        loops.push(Box::pin(accept_loop(listener, shutdown.clone()))
            as std::pin::Pin<Box<dyn Future<Output = ()> + Send>>);
    }

    // Every loop ends on the same signal, so they finish together.
    futures_util::future::join_all(loops).await;
    tracing::debug!("the metrics endpoint stopped");
}

async fn accept_loop<S, F>(listener: TcpListener, shutdown: S)
where
    S: Fn() -> F + Send,
    F: Future<Output = ()> + Send,
{
    loop {
        let accepted = tokio::select! {
            biased;
            () = shutdown() => break,
            result = listener.accept() => result,
        };

        let Ok((stream, _peer)) = accepted else {
            // A failed accept on the metrics port is not worth a retry policy
            // of its own; the next one usually succeeds.
            tokio::time::sleep(Duration::from_millis(50)).await;
            continue;
        };

        tokio::spawn(async move {
            let served = hyper_util::server::conn::auto::Builder::new(TokioExecutor::new())
                .serve_connection(TokioIo::new(stream), service_fn(answer))
                .await;
            if let Err(error) = served {
                tracing::debug!(%error, "a metrics scrape ended with an error");
            }
        });
    }
}

async fn answer(
    request: Request<hyper::body::Incoming>,
) -> Result<Response<Full<Bytes>>, hyper::Error> {
    if request.uri().path() != METRICS_PATH {
        return Ok(Response::builder()
            .status(StatusCode::NOT_FOUND)
            .header(hyper::header::CONTENT_TYPE, "text/plain; charset=utf-8")
            .body(Full::new(Bytes::from_static(b"not found\n")))
            .expect("a literal response is well formed"));
    }

    let body = metrics::render();
    Ok(Response::builder()
        .status(StatusCode::OK)
        .header(hyper::header::CONTENT_TYPE, EXPOSITION_TYPE)
        .body(Full::new(Bytes::from(body)))
        .expect("a rendered metrics response is well formed"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn enabled_on_ephemeral() -> ExporterConfig {
        ExporterConfig {
            enabled: true,
            host: "localhost".to_owned(),
            port: 0,
        }
    }

    #[test]
    fn the_defaults_are_off_and_loopback_only() {
        let config = ExporterConfig::default();

        assert!(!config.enabled, "metrics are opt-in");
        assert_eq!(config.host, "localhost");
        assert_ne!(
            config.port, 8000,
            "the exporter must not share the app port"
        );
        assert_eq!(config.port, 9464);
    }

    #[test]
    fn nothing_is_bound_when_it_is_switched_off() {
        let bound = bind(&ExporterConfig::default()).expect("binding nothing cannot fail");
        assert!(bound.is_empty());
        assert!(bound.addresses().is_empty());
    }

    #[test]
    fn enabling_it_binds_a_listener() {
        let bound = bind(&enabled_on_ephemeral()).expect("binding must work");
        assert!(!bound.is_empty());
        assert!(bound.addresses().iter().all(|address| address.port() != 0));
    }

    #[test]
    fn every_address_of_a_name_shares_one_port() {
        let bound = bind(&enabled_on_ephemeral()).expect("binding must work");
        let ports: std::collections::BTreeSet<u16> = bound
            .addresses()
            .iter()
            .map(|address| address.port())
            .collect();

        assert_eq!(ports.len(), 1, "one name means one port");
    }

    #[test]
    fn a_port_already_in_use_is_reported() {
        let first = bind(&enabled_on_ephemeral()).expect("the first bind must work");
        let port = first.addresses()[0].port();

        let error = bind(&ExporterConfig {
            enabled: true,
            host: "localhost".to_owned(),
            port,
        })
        .expect_err("a second bind on the same port must fail");

        assert!(matches!(error, ExporterError::Bind { .. }));
    }

    #[test]
    fn every_worker_gets_a_port_of_its_own() {
        let bound = bind_workers(&enabled_on_ephemeral(), 3).expect("binding must work");

        assert_eq!(bound.len(), 3);
        let ports: std::collections::BTreeSet<u16> = bound
            .iter()
            .flat_map(|exporter| exporter.addresses())
            .map(|address| address.port())
            .collect();
        assert_eq!(ports.len(), 3, "workers must not share a port");
    }

    #[test]
    fn worker_ports_follow_on_from_the_configured_one() {
        // Asking the kernel for a port, releasing it and then asking for that
        // exact number is a race: anything else on the machine may take it in
        // between, including another test in this binary. What is being tested
        // is that the second worker lands one port above the first, so a few
        // attempts with a fresh base each time is the honest way to get a base
        // that is free rather than a flake once a run.
        let mut last: Option<ExporterError> = None;

        for _ in 0..16 {
            let first = bind(&enabled_on_ephemeral()).expect("binding must work");
            let base = first.addresses()[0].port();
            drop(first);

            match bind_workers(
                &ExporterConfig {
                    enabled: true,
                    host: "localhost".to_owned(),
                    port: base,
                },
                2,
            ) {
                Ok(bound) => {
                    assert_eq!(bound[0].addresses()[0].port(), base);
                    assert_eq!(bound[1].addresses()[0].port(), base + 1);
                    return;
                }
                Err(error) => last = Some(error),
            }
        }

        panic!("no free consecutive pair of ports in 16 attempts: {last:?}");
    }

    #[test]
    fn no_worker_binds_anything_when_it_is_switched_off() {
        let bound = bind_workers(&ExporterConfig::default(), 4).expect("binding nothing works");

        assert!(bound.is_empty());
    }

    #[test]
    fn a_port_range_that_does_not_fit_is_reported() {
        let error = bind_workers(
            &ExporterConfig {
                enabled: true,
                host: "localhost".to_owned(),
                port: u16::MAX,
            },
            4,
        )
        .expect_err("a range past the last port must fail");

        assert!(matches!(error, ExporterError::TooManyWorkers { .. }));
    }

    #[test]
    fn descriptors_can_be_duplicated_for_a_worker() {
        let bound = bind(&enabled_on_ephemeral()).expect("binding must work");
        let copy = bound.duplicate().expect("duplicating must work");

        assert_eq!(bound.addresses(), copy.addresses());
    }
}
