//! HTTP/3 over QUIC, and the WebTransport sessions it carries.
//!
//! QUIC needs its own socket, its own listener and its own connection loop, but
//! not its own application interface: an HTTP/3 request reaches the same
//! [`Dispatch::handle_http`] as one that arrived over TCP, and a handler cannot
//! tell which it was.
//!
//! [`Dispatch::handle_http`]: crate::transport::Dispatch::handle_http

use std::io;
use std::net::{SocketAddr, ToSocketAddrs};
use std::sync::Arc;

use bytes::{Buf, Bytes};
use h3::ext::Protocol;
use http::{Method, Request, Response};
use http_body::Body;
use quinn::{Endpoint, EndpointConfig};
use socket2::{Domain, Protocol as SocketProtocol, Socket, Type};
use tokio::sync::mpsc;

use crate::config::ServerConfig;
use crate::disconnect::{DisconnectGuard, DisconnectWatcher};
use crate::headers::Headers;
use crate::lifecycle::drain::DrainCoordinator;
use crate::lifecycle::signals::ShutdownSignal;
use crate::transport::accept::BindError;
use crate::transport::tls::{TlsError, TlsMaterial};
use crate::transport::{
    BodyError, Dispatch, HttpRequest, HttpResponse, RequestBody, RequestParts, Scheme,
};
use crate::webtransport::{WebTransportRequest, WebTransportSession};

/// How many body chunks are held for a handler that is not reading yet.
const BODY_QUEUE_DEPTH: usize = 8;

/// How long a connection is given to close itself once its requests are done.
const CLOSE_GRACE: std::time::Duration = std::time::Duration::from_secs(3);

type H3Connection = h3::server::Connection<h3_quinn::Connection, Bytes>;

/// Bind one UDP socket per address the host resolves to.
///
/// As with TCP, `SO_REUSEPORT` is left off so an occupied port fails here
/// rather than silently starting a second listener.
pub fn bind_udp(host: &str, port: u16) -> Result<Vec<std::net::UdpSocket>, BindError> {
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

    addresses.into_iter().map(bind_one_udp).collect()
}

fn bind_one_udp(address: SocketAddr) -> Result<std::net::UdpSocket, BindError> {
    let describe = |source: io::Error| BindError::Socket {
        address: address.to_string(),
        source,
    };

    let domain = if address.is_ipv4() {
        Domain::IPV4
    } else {
        Domain::IPV6
    };
    let socket = Socket::new(domain, Type::DGRAM, Some(SocketProtocol::UDP)).map_err(describe)?;
    socket.set_reuse_address(true).map_err(describe)?;
    socket.bind(&address.into()).map_err(describe)?;
    socket.set_nonblocking(true).map_err(describe)?;

    tracing::info!(%address, "listening for QUIC");
    Ok(socket.into())
}

/// Turn bound sockets into QUIC endpoints. Must run inside the runtime that
/// will drive them.
pub fn endpoints(
    sockets: Vec<std::net::UdpSocket>,
    tls: &TlsMaterial,
) -> Result<Vec<Endpoint>, TlsError> {
    let crypto = quinn::crypto::rustls::QuicServerConfig::try_from(tls.quic_config()?)
        .map_err(|error| TlsError::Quic(error.to_string()))?;
    let server = quinn::ServerConfig::with_crypto(Arc::new(crypto));

    sockets
        .into_iter()
        .map(|socket| {
            Endpoint::new(
                EndpointConfig::default(),
                Some(server.clone()),
                socket,
                Arc::new(quinn::TokioRuntime),
            )
            .map_err(|error| TlsError::Quic(error.to_string()))
        })
        .collect()
}

/// Accept QUIC connections until shutdown is requested.
pub async fn accept<D: Dispatch>(
    endpoint: Endpoint,
    dispatch: D,
    config: Arc<ServerConfig>,
    drain: DrainCoordinator,
    shutdown: ShutdownSignal,
) {
    let server_address = endpoint.local_addr().ok();

    loop {
        let incoming = tokio::select! {
            biased;
            () = shutdown.wait() => break,
            incoming = endpoint.accept() => match incoming {
                Some(incoming) => incoming,
                None => break,
            },
        };

        let dispatch = dispatch.clone();
        let config = Arc::clone(&config);
        let drain = drain.clone();

        drain.clone().connections().spawn(async move {
            let client_address = incoming.remote_address();
            let connecting = match incoming.accept() {
                Ok(connecting) => connecting,
                Err(error) => {
                    tracing::debug!(%error, "a QUIC connection could not be accepted");
                    return;
                }
            };
            let connection = match connecting.await {
                Ok(connection) => connection,
                Err(error) => {
                    tracing::debug!(%client_address, %error, "the QUIC handshake failed");
                    return;
                }
            };

            serve_connection(
                connection,
                dispatch,
                config,
                drain,
                client_address,
                server_address,
            )
            .await;
        });
    }

    // Nothing new will be accepted; existing connections finish on their own.
    endpoint.close(0u32.into(), b"shutting down");
    tracing::debug!("QUIC accept loop stopped");
}

async fn serve_connection<D: Dispatch>(
    connection: quinn::Connection,
    dispatch: D,
    config: Arc<ServerConfig>,
    drain: DrainCoordinator,
    client: SocketAddr,
    server: Option<SocketAddr>,
) {
    let webtransport = config.webtransport;
    let closing = connection.clone();

    let mut h3 = match h3::server::builder()
        .enable_webtransport(webtransport)
        .enable_extended_connect(webtransport)
        .enable_datagram(webtransport)
        .max_webtransport_sessions(if webtransport { 1 } else { 0 })
        .send_grease(true)
        .build(h3_quinn::Connection::new(connection))
        .await
    {
        Ok(h3) => h3,
        Err(error) => {
            tracing::debug!(%client, %error, "the HTTP/3 layer could not be established");
            return;
        }
    };

    // Dropped when the connection ends, releasing handlers that were waiting on
    // it — the same contract a TCP connection offers.
    let guard = DisconnectGuard::new();
    let watcher = guard.watcher(drain.signal());
    let graceful = drain.graceful_signal();
    let mut requests: Vec<tokio::task::JoinHandle<()>> = Vec::new();

    loop {
        let resolver = tokio::select! {
            biased;
            () = graceful.wait() => break,
            accepted = h3.accept() => match accepted {
                Ok(Some(resolver)) => resolver,
                Ok(None) => break,
                Err(error) => {
                    tracing::debug!(%client, %error, "the HTTP/3 connection ended");
                    break;
                }
            },
        };

        let (request, stream) = match resolver.resolve_request().await {
            Ok(resolved) => resolved,
            Err(error) => {
                tracing::debug!(%client, %error, "an HTTP/3 request could not be read");
                break;
            }
        };

        let parts = parts_from(&request, client, server);

        if webtransport && is_webtransport(&request) {
            // The session takes over the whole connection, so nothing else can
            // be served on it afterwards.
            serve_webtransport(
                request,
                stream,
                h3,
                dispatch,
                parts,
                watcher,
                config.datagram_queue_capacity,
            )
            .await;
            return;
        }

        let dispatch = dispatch.clone();
        let watcher = watcher.clone();
        requests.push(tokio::spawn(async move {
            if let Err(error) = serve_request(stream, dispatch, parts, watcher).await {
                tracing::debug!(%error, "an HTTP/3 request failed");
            }
        }));
    }

    for request in requests {
        let _finished = request.await;
    }
    drop(guard);

    // Letting the peer close avoids resetting a stream it has just been served
    // on; if it does not, the timeout ends the wait.
    let _closed = tokio::time::timeout(CLOSE_GRACE, closing.closed()).await;
}

fn is_webtransport(request: &Request<()>) -> bool {
    request.method() == Method::CONNECT
        && request.extensions().get::<Protocol>() == Some(&Protocol::WEB_TRANSPORT)
}

fn parts_from(
    request: &Request<()>,
    client: SocketAddr,
    server: Option<SocketAddr>,
) -> RequestParts {
    RequestParts {
        method: request.method().clone(),
        uri: request.uri().clone(),
        version: http::Version::HTTP_3,
        headers: Headers::from(request.headers().clone()),
        // QUIC always carries TLS, so an HTTP/3 request is always secure.
        scheme: Scheme::Https,
        client: Some(client),
        server,
    }
}

async fn serve_request<D: Dispatch>(
    stream: h3::server::RequestStream<h3_quinn::BidiStream<Bytes>, Bytes>,
    dispatch: D,
    parts: RequestParts,
    disconnect: DisconnectWatcher,
) -> Result<(), String> {
    let (mut sender, mut receiver) = stream.split();

    // The body is read by a task of its own so a handler that never reads it
    // does not stall the stream, and one that reads slowly applies real
    // backpressure through the bounded channel.
    let (chunks, body) = mpsc::channel(BODY_QUEUE_DEPTH);
    tokio::spawn(async move {
        loop {
            match receiver.recv_data().await {
                Ok(Some(mut chunk)) => {
                    let bytes = chunk.copy_to_bytes(chunk.remaining());
                    if chunks.send(Ok(bytes)).await.is_err() {
                        break;
                    }
                }
                Ok(None) => break,
                Err(error) => {
                    let _sent = chunks.send(Err(BodyError::new(error))).await;
                    break;
                }
            }
        }
    });

    let response = dispatch
        .handle_http(HttpRequest {
            parts,
            body: RequestBody::Chunks(body),
            disconnect,
        })
        .await;

    let HttpResponse {
        status,
        headers,
        body,
    } = response;

    let mut head = Response::new(());
    *head.status_mut() = status;
    *head.headers_mut() = headers.into_map();
    sender
        .send_response(head)
        .await
        .map_err(|error| error.to_string())?;

    let mut body = std::pin::pin!(body.into_boxed());
    while let Some(frame) = std::future::poll_fn(|context| body.as_mut().poll_frame(context)).await
    {
        match frame {
            Ok(frame) => {
                if let Ok(chunk) = frame.into_data()
                    && sender.send_data(chunk).await.is_err()
                {
                    break;
                }
            }
            Err(error) => {
                tracing::debug!(%error, "an HTTP/3 response body failed mid-send");
                break;
            }
        }
    }

    sender.finish().await.map_err(|error| error.to_string())
}

#[allow(clippy::too_many_arguments)]
async fn serve_webtransport<D: Dispatch>(
    request: Request<()>,
    stream: h3::server::RequestStream<h3_quinn::BidiStream<Bytes>, Bytes>,
    connection: H3Connection,
    dispatch: D,
    parts: RequestParts,
    disconnect: DisconnectWatcher,
    datagram_capacity: usize,
) {
    let session = WebTransportSession::pending(request, stream, connection, datagram_capacity);

    dispatch
        .handle_webtransport(WebTransportRequest {
            parts,
            session,
            disconnect,
        })
        .await;
    tracing::debug!("the WebTransport session ended");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn binding_reports_the_chosen_port() {
        let sockets = bind_udp("127.0.0.1", 0).expect("binding an ephemeral port must work");
        assert_eq!(sockets.len(), 1);
        assert_ne!(sockets[0].local_addr().unwrap().port(), 0);
    }

    #[test]
    fn a_port_already_in_use_fails_at_bind_time() {
        let first = bind_udp("127.0.0.1", 0).expect("the first bind must work");
        let port = first[0].local_addr().unwrap().port();

        assert!(
            bind_udp("127.0.0.1", port).is_err(),
            "a second bind on the same UDP port must fail"
        );
    }

    #[test]
    fn an_unresolvable_host_is_reported() {
        assert!(matches!(
            bind_udp("host.invalid.", 8000),
            Err(BindError::Unresolvable { .. }) | Err(BindError::Socket { .. })
        ));
    }

    #[test]
    fn an_extended_connect_for_webtransport_is_recognised() {
        let mut request = Request::new(());
        *request.method_mut() = Method::CONNECT;
        request.extensions_mut().insert(Protocol::WEB_TRANSPORT);
        assert!(is_webtransport(&request));

        let mut ordinary = Request::new(());
        *ordinary.method_mut() = Method::GET;
        assert!(!is_webtransport(&ordinary));
    }

    #[test]
    fn a_plain_connect_is_not_webtransport() {
        let mut request = Request::new(());
        *request.method_mut() = Method::CONNECT;
        assert!(!is_webtransport(&request));
    }
}
