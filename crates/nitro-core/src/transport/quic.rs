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

use crate::disconnect::{DisconnectGuard, DisconnectWatcher};
use crate::headers::Headers;
use crate::lifecycle::drain::DrainCoordinator;
use crate::lifecycle::signals::ShutdownSignal;
use crate::transport::accept::BindError;
use crate::transport::connection::{self, ConnectionContext};
use crate::transport::tls::{TlsError, TlsMaterial};
use crate::transport::{
    BodyError, Dispatch, HttpRequest, HttpResponse, RequestBody, RequestParts, Scheme,
};
use crate::webtransport::{ConnectionKeeper, WebTransportRequest, WebTransportSession};
use nitro_observability::metrics;

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

    // As with TCP, one name means one port: the kernel would otherwise give a
    // different ephemeral port to each address the name resolves to.
    let mut sockets = Vec::with_capacity(addresses.len());
    let mut chosen = port;

    for mut address in addresses {
        if chosen != 0 {
            address.set_port(chosen);
        }
        let socket = bind_one_udp(address)?;
        if chosen == 0 {
            chosen = socket.local_addr().map(|bound| bound.port()).unwrap_or(0);
        }
        sockets.push(socket);
    }
    Ok(sockets)
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
    // No SO_REUSEADDR here, unlike the TCP listener. On TCP it only permits
    // rebinding a port still in TIME_WAIT, which is what lets a restart happen
    // immediately. UDP has no such state, and on Linux the option instead lets
    // a second process bind a port that is already serving — two servers would
    // then split datagrams between them, each apparently having started
    // cleanly. A port already in use should be one clear error instead.
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
    context: ConnectionContext<D>,
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

        let context = context.clone();
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

            metrics::connection_opened(metrics::Transport::Quic);
            serve_connection(connection, context, drain, client_address, server_address).await;
            metrics::connection_closed(metrics::Transport::Quic);
        });
    }

    // Nothing new will be accepted; existing connections finish on their own.
    endpoint.close(0u32.into(), b"shutting down");
    tracing::debug!("QUIC accept loop stopped");
}

async fn serve_connection<D: Dispatch>(
    connection: quinn::Connection,
    context: ConnectionContext<D>,
    drain: DrainCoordinator,
    client: SocketAddr,
    server: Option<SocketAddr>,
) {
    let webtransport = context.config().webtransport;
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

        // The same refusal a request over TCP gets, and for the same reason:
        // nothing downstream should see a host name the deployment did not
        // configure. A WebTransport session is refused here too — its CONNECT
        // carries an authority like any other request.
        if !context.config().allowed_hosts.permits(parts.authority()) {
            tracing::debug!(
                %client,
                host = ?parts.authority(),
                "refusing a request for an unconfigured host"
            );
            let refused = send_plain(stream, connection::host_refusal()).await;
            if let Err(error) = refused {
                tracing::debug!(%client, %error, "could not refuse an unconfigured host");
            }
            continue;
        }

        if webtransport && is_webtransport(&request) {
            // The session takes over the whole connection, so nothing else can
            // be served on it afterwards.
            let keeper = serve_webtransport(
                request,
                stream,
                h3,
                context.dispatch().clone(),
                parts,
                watcher,
                context.config().datagram_queue_capacity,
            )
            .await;

            // The same close as any other connection, rather than returning
            // here: a handler that refused the session has just written a
            // response, and closing now would cut it off before the client saw
            // it.
            drop(guard);
            let _closed = tokio::time::timeout(CLOSE_GRACE, closing.closed()).await;
            drop(keeper);
            return;
        }

        let context = context.clone();
        let watcher = watcher.clone();
        requests.push(tokio::spawn(async move {
            if let Err(error) = serve_request(stream, context, parts, watcher).await {
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

/// Write a short response and finish the stream.
///
/// For answers the server produces without the application: no access log
/// entry, no metrics, no body streaming — the whole thing is a status and a
/// sentence.
async fn send_plain(
    stream: h3::server::RequestStream<h3_quinn::BidiStream<Bytes>, Bytes>,
    response: HttpResponse,
) -> Result<(), String> {
    let HttpResponse {
        status,
        headers,
        body,
        route: _,
    } = response;

    let (mut sender, _receiver) = stream.split();
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
            Err(error) => return Err(error.to_string()),
        }
    }

    sender.finish().await.map_err(|error| error.to_string())
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
    context: ConnectionContext<D>,
    parts: RequestParts,
    disconnect: DisconnectWatcher,
) -> Result<(), String> {
    let method = parts.method.clone();
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

    let started = std::time::Instant::now();
    metrics::request_started();

    let logged = context.summarise(&parts);

    let response = context
        .dispatch()
        .handle_http(HttpRequest {
            parts,
            body: RequestBody::Chunks(body),
            disconnect,
        })
        .await;

    metrics::request_finished();
    metrics::record_request(
        response.route.as_deref(),
        method.as_str(),
        response.status.as_u16(),
        started.elapsed(),
    );

    let HttpResponse {
        status,
        headers,
        body,
        route: _,
    } = response;

    let content_length = body.content_length();
    let mut head = Response::new(());
    *head.status_mut() = status;
    *head.headers_mut() = headers.into_map();

    // The same headers a response over TCP is given, including the length —
    // which a HEAD response is described by even though it is not sent.
    context.decorate(head.headers_mut(), status, content_length);
    context.log_access(logged, status.as_u16(), content_length, started.elapsed());

    sender
        .send_response(head)
        .await
        .map_err(|error| error.to_string())?;

    // RFC 9110 §9.3.2: a HEAD response carries the header fields a GET would
    // and no content. Over TCP hyper drops the body itself; here the frames are
    // written by hand, so the check has to be too — and a client that receives
    // DATA on a HEAD response resets the stream rather than reading it.
    if method != Method::HEAD {
        let mut body = std::pin::pin!(body.into_boxed());
        while let Some(frame) =
            std::future::poll_fn(|context| body.as_mut().poll_frame(context)).await
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
    }

    sender.finish().await.map_err(|error| error.to_string())
}

/// Returns whatever is left holding the connection, so the caller can keep it
/// open while the last frames are flushed.
#[allow(clippy::too_many_arguments)]
async fn serve_webtransport<D: Dispatch>(
    request: Request<()>,
    stream: h3::server::RequestStream<h3_quinn::BidiStream<Bytes>, Bytes>,
    connection: H3Connection,
    dispatch: D,
    parts: RequestParts,
    disconnect: DisconnectWatcher,
    datagram_capacity: usize,
) -> ConnectionKeeper {
    // Held here rather than by the session, so that a refusal — which drops
    // the session as soon as the handler returns — still has an open
    // connection to travel over.
    let keeper: ConnectionKeeper = Arc::new(std::sync::Mutex::new(Some(connection)));
    let session =
        WebTransportSession::pending(request, stream, Arc::clone(&keeper), datagram_capacity);

    dispatch
        .handle_webtransport(WebTransportRequest {
            parts,
            session,
            disconnect,
        })
        .await;
    tracing::debug!("the WebTransport session ended");
    keeper
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
