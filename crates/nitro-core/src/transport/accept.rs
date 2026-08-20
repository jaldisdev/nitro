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

//! Socket binding and accept loops.
//!
//! Binding is separated from serving because the two happen in different
//! processes: the parent binds every socket once, so a port clash is reported
//! exactly once and before any worker exists, and each worker then serves the
//! descriptors it inherited.

use std::io;
use std::net::{SocketAddr, ToSocketAddrs};
#[cfg(unix)]
use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use socket2::{Domain, Protocol, Socket, Type};
use tokio::net::TcpListener;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

use crate::config::{BindAddress, ServerConfig};
use crate::lifecycle::drain::{DrainCoordinator, DrainOutcome};
use crate::lifecycle::signals::ShutdownSignal;
use crate::transport::connection::{self, ConnectionContext};
use crate::transport::quic;
use crate::transport::tls::{TlsError, TlsMaterial};
use crate::transport::{Dispatch, Scheme};
use nitro_observability::exporter::{self, BoundExporter};

/// How long the accept loop pauses after an error that is worth retrying, so a
/// process at its descriptor limit does not spin at full speed.
const ACCEPT_RETRY_DELAY: Duration = Duration::from_millis(50);

#[derive(Debug, thiserror::Error)]
pub enum BindError {
    #[error("'{host}' did not resolve to any address")]
    Unresolvable { host: String },
    #[error("binding {address}: {source}")]
    Socket {
        address: String,
        #[source]
        source: io::Error,
    },
    #[error("removing the stale socket file {path}: {source}")]
    StaleSocket {
        path: PathBuf,
        #[source]
        source: io::Error,
    },
}

#[derive(Debug, thiserror::Error)]
pub enum ServeError {
    #[error(transparent)]
    Tls(#[from] TlsError),
    #[error("the listening socket became unusable: {0}")]
    Listener(#[from] io::Error),
}

/// Listening sockets, bound but not yet accepting.
#[derive(Debug, Default)]
pub struct BoundSockets {
    pub tcp: Vec<std::net::TcpListener>,
    #[cfg(unix)]
    pub unix: Option<std::os::unix::net::UnixListener>,
    /// UDP sockets for QUIC, bound only when HTTP/3 is enabled.
    pub quic: Vec<std::net::UdpSocket>,
    /// One metrics listener per worker, each on a port of its own. Empty
    /// unless observability is switched on.
    ///
    /// A forked worker keeps only the entry for its own index; a process that
    /// serves this set directly serves everything in it.
    pub metrics: Vec<BoundExporter>,
}

impl BoundSockets {
    pub fn is_empty(&self) -> bool {
        #[cfg(unix)]
        {
            self.tcp.is_empty() && self.unix.is_none() && self.quic.is_empty()
        }
        #[cfg(not(unix))]
        {
            self.tcp.is_empty() && self.quic.is_empty()
        }
    }

    /// The addresses actually bound, for logging and for tests that ask the
    /// kernel to choose a port.
    pub fn local_addresses(&self) -> Vec<SocketAddr> {
        self.tcp
            .iter()
            .filter_map(|listener| listener.local_addr().ok())
            .collect()
    }
}

/// Bind every socket the configuration calls for.
///
/// `SO_REUSEPORT` is deliberately not set. Workers inherit these descriptors
/// rather than binding their own, so nothing needs it — and leaving it off is
/// what makes a port that is already taken fail here, loudly, instead of
/// quietly starting a second server alongside the first.
pub fn bind(config: &ServerConfig) -> Result<BoundSockets, BindError> {
    match &config.bind {
        BindAddress::Tcp { host, port } => {
            let (tcp, quic) = if *port == 0 {
                bind_ephemeral(config, host)?
            } else {
                bind_on_port(config, host, *port)?
            };

            Ok(BoundSockets {
                #[cfg(unix)]
                unix: None,
                quic,
                metrics: bind_metrics(config)?,
                tcp,
            })
        }
        #[cfg(unix)]
        BindAddress::Unix { path } => Ok(BoundSockets {
            tcp: Vec::new(),
            unix: Some(bind_unix(path, config.backlog)?),
            quic: Vec::new(),
            metrics: bind_metrics(config)?,
        }),
        #[cfg(not(unix))]
        BindAddress::Unix { path } => Err(BindError::Socket {
            address: path.display().to_string(),
            source: io::Error::new(
                io::ErrorKind::Unsupported,
                "Unix domain sockets are not available on this platform",
            ),
        }),
    }
}

/// How many ephemeral ports are tried before giving up.
///
/// Each attempt fails only if something took the port between the kernel
/// choosing it and the rest of the sockets being bound, so needing even a
/// second attempt is rare and needing sixteen means the machine has no free
/// ports rather than that this is unlucky.
const EPHEMERAL_ATTEMPTS: usize = 16;

/// Bind the listeners for a fixed port.
fn bind_on_port(
    config: &ServerConfig,
    host: &str,
    port: u16,
) -> Result<(Vec<std::net::TcpListener>, Vec<std::net::UdpSocket>), BindError> {
    let tcp = bind_tcp(host, port, config.backlog)?;
    let quic = if config.http.h3_enabled() {
        quic::bind_udp(host, port)?
    } else {
        Vec::new()
    };
    Ok((tcp, quic))
}

/// Bind the listeners on a port the kernel chooses.
///
/// One name usually resolves to several addresses, and HTTP/3 has to answer on
/// the same port as the TCP listener that advertises it — so one port has to be
/// free across every address and both protocols at once. The kernel only
/// promises that for the single socket it picked the port for; anything else
/// may hold it. That is a real gap rather than a test artefact: a server
/// started on port zero could fail to bind for no reason the operator can see.
///
/// So the whole set is attempted together and retried on a fresh port when
/// something else already holds one of them.
fn bind_ephemeral(
    config: &ServerConfig,
    host: &str,
) -> Result<(Vec<std::net::TcpListener>, Vec<std::net::UdpSocket>), BindError> {
    let mut last: Option<BindError> = None;

    for _ in 0..EPHEMERAL_ATTEMPTS {
        let tcp = match bind_tcp(host, 0, config.backlog) {
            Ok(tcp) => tcp,
            Err(error) if is_taken(&error) => {
                last = Some(error);
                continue;
            }
            Err(error) => return Err(error),
        };

        if !config.http.h3_enabled() {
            return Ok((tcp, Vec::new()));
        }

        let chosen = tcp
            .first()
            .and_then(|listener| listener.local_addr().ok())
            .map(|address| address.port())
            .unwrap_or(0);

        match quic::bind_udp(host, chosen) {
            Ok(quic) => return Ok((tcp, quic)),
            Err(error) if is_taken(&error) => {
                // Dropping the TCP listeners releases the port before the next
                // attempt, so a run of attempts cannot exhaust the range.
                drop(tcp);
                last = Some(error);
            }
            Err(error) => return Err(error),
        }
    }

    Err(last.unwrap_or_else(|| BindError::Unresolvable {
        host: host.to_owned(),
    }))
}

/// Whether a bind failed because something else already holds the address.
fn is_taken(error: &BindError) -> bool {
    matches!(
        error,
        BindError::Socket { source, .. } if source.kind() == io::ErrorKind::AddrInUse
    )
}

fn bind_metrics(config: &ServerConfig) -> Result<Vec<BoundExporter>, BindError> {
    exporter::bind_workers(&config.observability, config.workers).map_err(|error| {
        BindError::Socket {
            address: format!(
                "{}:{}",
                config.observability.host, config.observability.port
            ),
            source: io::Error::other(error.to_string()),
        }
    })
}

fn bind_tcp(host: &str, port: u16, backlog: u32) -> Result<Vec<std::net::TcpListener>, BindError> {
    let addresses: Vec<SocketAddr> = format!("{host}:{port}")
        .to_socket_addrs()
        .map_err(|source| BindError::Socket {
            address: format!("{host}:{port}"),
            source,
        })?
        .collect();

    if addresses.is_empty() {
        return Err(BindError::Unresolvable {
            host: host.to_owned(),
        });
    }

    // A host name usually resolves to more than one address — `localhost` to
    // both loopback families — and each gets a socket. Asking for port zero
    // means "any free port", but the kernel would then choose a *different*
    // one per socket, so a client resolving the name to the other family would
    // find nothing there. The port the first socket is given is therefore
    // reused for the rest, so one name means one port.
    let mut listeners = Vec::with_capacity(addresses.len());
    let mut chosen = port;

    for mut address in addresses {
        if chosen != 0 {
            address.set_port(chosen);
        }
        let listener = bind_one_tcp(address, backlog)?;
        if chosen == 0 {
            chosen = listener.local_addr().map(|bound| bound.port()).unwrap_or(0);
        }
        listeners.push(listener);
    }
    Ok(listeners)
}

fn bind_one_tcp(address: SocketAddr, backlog: u32) -> Result<std::net::TcpListener, BindError> {
    let describe = |source: io::Error| BindError::Socket {
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
    socket
        .listen(backlog.min(i32::MAX as u32) as i32)
        .map_err(describe)?;
    socket.set_nonblocking(true).map_err(describe)?;

    tracing::info!(%address, "listening");
    Ok(socket.into())
}

#[cfg(unix)]
fn bind_unix(path: &Path, backlog: u32) -> Result<std::os::unix::net::UnixListener, BindError> {
    use std::os::unix::net::UnixListener;

    // A socket file left behind by a process that did not shut down cleanly
    // would otherwise make every subsequent start fail.
    match std::fs::remove_file(path) {
        Ok(()) => tracing::warn!(?path, "removed a leftover socket file"),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(source) => {
            return Err(BindError::StaleSocket {
                path: path.to_path_buf(),
                source,
            });
        }
    }

    let describe = |source: io::Error| BindError::Socket {
        address: path.display().to_string(),
        source,
    };

    let socket = Socket::new(Domain::UNIX, Type::STREAM, None).map_err(describe)?;
    let address = socket2::SockAddr::unix(path).map_err(describe)?;
    socket.bind(&address).map_err(describe)?;
    socket
        .listen(backlog.min(i32::MAX as u32) as i32)
        .map_err(describe)?;
    socket.set_nonblocking(true).map_err(describe)?;

    tracing::info!(?path, "listening");
    Ok(UnixListener::from(socket))
}

/// Serve every bound socket until shutdown is requested, then run the drain
/// chain and report how it went.
pub async fn serve<D: Dispatch>(
    sockets: BoundSockets,
    dispatch: D,
    config: Arc<ServerConfig>,
    tls: Option<TlsMaterial>,
    shutdown: ShutdownSignal,
    drain: DrainCoordinator,
) -> Result<DrainOutcome, ServeError> {
    let tls_acceptor = match (&tls, config.tcp_tls().is_some()) {
        (Some(material), true) => Some(material.tcp_acceptor(config.http)?),
        _ => None,
    };
    let scheme = if tls_acceptor.is_some() {
        Scheme::Https
    } else {
        Scheme::Http
    };

    if let Some(material) = &tls
        && let Some(reloader) = material.spawn_reloader(shutdown.clone())
    {
        drain.tracker().spawn(async move {
            if let Err(error) = reloader.await
                && !error.is_cancelled()
            {
                tracing::error!(%error, "the certificate reloader stopped unexpectedly");
            }
        });
    }

    let served_port = sockets
        .local_addresses()
        .first()
        .map(|address| address.port());
    let context =
        ConnectionContext::new(dispatch, Arc::clone(&config), drain.signal(), served_port);
    nitro_observability::metrics::worker_started();
    let limit = config
        .max_concurrent_connections
        .map(|maximum| Arc::new(Semaphore::new(maximum)));

    let mut loops: Vec<std::pin::Pin<Box<dyn Future<Output = ()> + Send>>> = Vec::new();
    for listener in sockets.tcp {
        listener.set_nonblocking(true)?;
        let listener = TcpListener::from_std(listener)?;
        loops.push(Box::pin(accept_tcp(
            listener,
            tls_acceptor.clone(),
            scheme,
            context.clone(),
            drain.clone(),
            shutdown.clone(),
            limit.clone(),
        )));
    }

    #[cfg(unix)]
    if let Some(listener) = sockets.unix {
        listener.set_nonblocking(true)?;
        let listener = tokio::net::UnixListener::from_std(listener)?;
        loops.push(Box::pin(accept_unix(
            listener,
            context.clone(),
            drain.clone(),
            shutdown.clone(),
            limit.clone(),
        )));
    }

    if !sockets.quic.is_empty() {
        let Some(material) = &tls else {
            return Err(ServeError::Tls(TlsError::Quic(
                "HTTP/3 needs a certificate".to_owned(),
            )));
        };
        for endpoint in quic::endpoints(sockets.quic, material)? {
            loops.push(Box::pin(quic::accept(
                endpoint,
                context.clone(),
                drain.clone(),
                shutdown.clone(),
            )));
        }
    }

    for endpoint in sockets.metrics {
        let shutdown = shutdown.clone();
        loops.push(Box::pin(exporter::serve(endpoint, move || {
            let shutdown = shutdown.clone();
            async move { shutdown.wait().await }
        })));
    }

    if loops.is_empty() {
        tracing::warn!("no listening sockets were provided; nothing to serve");
    }

    futures_util::future::join_all(loops).await;

    Ok(drain.drain().await)
}

#[allow(clippy::too_many_arguments)]
async fn accept_tcp<D: Dispatch>(
    listener: TcpListener,
    tls: Option<tokio_rustls::TlsAcceptor>,
    scheme: Scheme,
    context: ConnectionContext<D>,
    drain: DrainCoordinator,
    shutdown: ShutdownSignal,
    limit: Option<Arc<Semaphore>>,
) {
    let server_address = listener.local_addr().ok();
    let graceful = drain.graceful_signal();

    loop {
        let Some(permit) = acquire_slot(&limit, &shutdown).await else {
            break;
        };

        let accepted = tokio::select! {
            biased;
            () = shutdown.wait() => break,
            result = listener.accept() => result,
        };

        let (stream, client_address) = match accepted {
            Ok(accepted) => accepted,
            Err(error) => {
                if !retry_after(&error).await {
                    tracing::error!(%error, "the TCP accept loop is giving up");
                    break;
                }
                continue;
            }
        };

        if let Err(error) = stream.set_nodelay(true) {
            tracing::debug!(%error, "could not disable Nagle's algorithm");
        }

        let context = context.clone();
        let tls = tls.clone();
        let graceful = graceful.clone();
        drain.connections().spawn(async move {
            let _permit = permit;
            connection::serve_tcp(
                stream,
                client_address,
                server_address,
                scheme,
                tls,
                context,
                graceful,
            )
            .await;
        });
    }

    tracing::debug!("TCP accept loop stopped");
}

#[cfg(unix)]
async fn accept_unix<D: Dispatch>(
    listener: tokio::net::UnixListener,
    context: ConnectionContext<D>,
    drain: DrainCoordinator,
    shutdown: ShutdownSignal,
    limit: Option<Arc<Semaphore>>,
) {
    let graceful = drain.graceful_signal();

    loop {
        let Some(permit) = acquire_slot(&limit, &shutdown).await else {
            break;
        };

        let accepted = tokio::select! {
            biased;
            () = shutdown.wait() => break,
            result = listener.accept() => result,
        };

        let (stream, _address) = match accepted {
            Ok(accepted) => accepted,
            Err(error) => {
                if !retry_after(&error).await {
                    tracing::error!(%error, "the Unix socket accept loop is giving up");
                    break;
                }
                continue;
            }
        };

        let context = context.clone();
        let graceful = graceful.clone();
        drain.connections().spawn(async move {
            let _permit = permit;
            connection::serve_unix(stream, context, graceful).await;
        });
    }

    tracing::debug!("Unix socket accept loop stopped");
}

/// Wait for a connection slot. Returns `None` when the loop should stop.
///
/// The permit is taken before the accept call rather than after, so a server at
/// its limit leaves connections queued in the kernel backlog — where a client
/// still sees a normal wait — instead of accepting them and holding them open
/// with nobody to serve them.
async fn acquire_slot(
    limit: &Option<Arc<Semaphore>>,
    shutdown: &ShutdownSignal,
) -> Option<Option<OwnedSemaphorePermit>> {
    let Some(semaphore) = limit else {
        return Some(None);
    };

    tokio::select! {
        biased;
        () = shutdown.wait() => None,
        permit = Arc::clone(semaphore).acquire_owned() => match permit {
            Ok(permit) => Some(Some(permit)),
            Err(_closed) => None,
        },
    }
}

/// Whether an accept error is worth retrying, pausing first if so.
///
/// Running out of descriptors or buffers is a temporary condition that clears
/// as connections close, so the loop keeps going; anything else means the
/// listener itself is broken and retrying would spin forever.
async fn retry_after(error: &io::Error) -> bool {
    let transient = matches!(
        error.kind(),
        io::ErrorKind::ConnectionAborted
            | io::ErrorKind::ConnectionReset
            | io::ErrorKind::Interrupted
            | io::ErrorKind::WouldBlock
            | io::ErrorKind::OutOfMemory
    ) || matches!(error.raw_os_error(), Some(libc_emfile) if libc_emfile == EMFILE || libc_emfile == ENFILE);

    if transient {
        tracing::warn!(%error, "accept failed; retrying");
        tokio::time::sleep(ACCEPT_RETRY_DELAY).await;
    }
    transient
}

#[cfg(unix)]
const EMFILE: i32 = 24;
#[cfg(unix)]
const ENFILE: i32 = 23;
#[cfg(not(unix))]
const EMFILE: i32 = i32::MIN;
#[cfg(not(unix))]
const ENFILE: i32 = i32::MIN + 1;

#[cfg(test)]
mod tests {
    use super::*;

    fn ephemeral_config() -> ServerConfig {
        ServerConfig {
            bind: BindAddress::tcp("127.0.0.1", 0),
            http: crate::config::HttpVersion::Http1,
            ..Default::default()
        }
    }

    #[test]
    fn binding_reports_the_chosen_port() {
        let sockets = bind(&ephemeral_config()).expect("binding an ephemeral port must work");
        let addresses = sockets.local_addresses();
        assert_eq!(addresses.len(), 1);
        assert_ne!(addresses[0].port(), 0);
        assert!(!sockets.is_empty());
    }

    #[test]
    fn a_port_already_in_use_fails_at_bind_time() {
        let first = bind(&ephemeral_config()).expect("the first bind must work");
        let port = first.local_addresses()[0].port();

        let mut config = ephemeral_config();
        config.bind = BindAddress::tcp("127.0.0.1", port);

        let error = bind(&config).expect_err("a second bind on the same port must fail");
        assert!(matches!(error, BindError::Socket { .. }));
    }

    #[test]
    fn an_unresolvable_host_is_reported() {
        let mut config = ephemeral_config();
        config.bind = BindAddress::tcp("host.invalid.", 8000);
        assert!(bind(&config).is_err());
    }

    #[test]
    fn a_name_resolving_to_several_addresses_uses_one_port() {
        let mut config = ephemeral_config();
        config.bind = BindAddress::tcp("localhost", 0);

        let sockets = bind(&config).expect("binding localhost must work");
        let addresses = sockets.local_addresses();
        assert!(!addresses.is_empty());

        let ports: std::collections::BTreeSet<u16> =
            addresses.iter().map(|address| address.port()).collect();
        assert_eq!(
            ports.len(),
            1,
            "every address a name resolves to must be served on the same port, got {addresses:?}"
        );
    }

    #[test]
    fn quic_follows_the_port_tcp_was_given() {
        let mut config = ephemeral_config();
        config.http = crate::config::HttpVersion::Http3;

        let sockets = bind(&config).expect("binding must work");
        let tcp_port = sockets.local_addresses()[0].port();
        let quic_port = sockets.quic[0].local_addr().unwrap().port();

        assert_eq!(
            tcp_port, quic_port,
            "HTTP/3 must be reachable on the port it is advertised from"
        );
    }

    #[test]
    fn an_ephemeral_bind_survives_a_port_taken_between_choosing_and_binding() {
        // One name resolves to several addresses and HTTP/3 needs the same
        // port as TCP, so the kernel's promise about the single socket it
        // chose a port for is not enough: anything may hold that number on
        // another address or on UDP. Binding repeatedly is the closest a test
        // can get to the race without arranging it.
        for _ in 0..24 {
            let mut config = ephemeral_config();
            config.bind = BindAddress::tcp("localhost", 0);
            config.http = crate::config::HttpVersion::Http3;

            let sockets = bind(&config).expect("an ephemeral bind must not fail");
            let tcp_port = sockets.local_addresses()[0].port();

            assert!(
                sockets.quic.iter().all(|socket| socket
                    .local_addr()
                    .ok()
                    .map(|address| address.port())
                    == Some(tcp_port)),
                "HTTP/3 must answer on the port TCP advertises"
            );
        }
    }

    #[test]
    fn quic_uses_one_port_across_a_name_too() {
        let mut config = ephemeral_config();
        config.bind = BindAddress::tcp("localhost", 0);
        config.http = crate::config::HttpVersion::Http3;

        let sockets = bind(&config).expect("binding localhost must work");
        let tcp: std::collections::BTreeSet<u16> = sockets
            .local_addresses()
            .iter()
            .map(|address| address.port())
            .collect();
        let quic: std::collections::BTreeSet<u16> = sockets
            .quic
            .iter()
            .filter_map(|socket| socket.local_addr().ok())
            .map(|address| address.port())
            .collect();

        assert_eq!(tcp, quic, "HTTP/3 must be reachable wherever TCP is");
        assert_eq!(tcp.len(), 1);
    }

    #[test]
    fn no_udp_socket_is_bound_without_http3() {
        let sockets = bind(&ephemeral_config()).expect("binding must work");
        assert!(sockets.quic.is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn a_unix_socket_binds_and_replaces_a_stale_file() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("nitro.sock");
        std::fs::write(&path, b"stale").unwrap();

        let mut config = ephemeral_config();
        config.bind = BindAddress::Unix { path: path.clone() };

        let sockets = bind(&config).expect("a stale socket file must not block binding");
        assert!(sockets.unix.is_some());
        assert!(sockets.tcp.is_empty());
        assert!(path.exists());
    }

    #[tokio::test]
    async fn transient_accept_errors_are_retried() {
        assert!(retry_after(&io::Error::from(io::ErrorKind::ConnectionAborted)).await);
        assert!(retry_after(&io::Error::from_raw_os_error(EMFILE)).await);
        assert!(!retry_after(&io::Error::from(io::ErrorKind::InvalidInput)).await);
    }

    #[tokio::test]
    async fn slot_acquisition_stops_when_shutdown_is_requested() {
        let semaphore = Arc::new(Semaphore::new(1));
        let controller = crate::lifecycle::signals::ShutdownController::new();
        let shutdown = controller.subscribe();

        let held = Arc::clone(&semaphore).acquire_owned().await.unwrap();
        controller.trigger();

        assert!(acquire_slot(&Some(semaphore), &shutdown).await.is_none());
        drop(held);
    }

    #[tokio::test]
    async fn an_unlimited_server_never_waits_for_a_slot() {
        let shutdown = ShutdownSignal::never();
        let slot = acquire_slot(&None, &shutdown).await;
        assert!(matches!(slot, Some(None)));
    }
}
