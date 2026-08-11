//! Per-connection serving.
//!
//! One connection carries one disconnect guard and any number of exchanges. The
//! guard is dropped when the connection ends for any reason, which is what
//! releases handlers still waiting on it.

use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::{Arc, LazyLock, RwLock};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use bytes::Bytes;
use http::{HeaderValue, Request, Response, StatusCode};
use http_body_util::combinators::BoxBody;
use hyper::body::Incoming;
use hyper::service::Service;
use hyper_util::rt::{TokioExecutor, TokioIo};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio_rustls::TlsAcceptor;

use crate::config::{ConnectionAddresses, ServerConfig};
use crate::disconnect::{DisconnectGuard, DisconnectSignal, DisconnectWatcher};
use crate::headers::Headers;
use crate::lifecycle::{AccessLogger, AccessRecord};
use crate::streaming::StreamError;
use crate::transport::{
    Dispatch, HttpRequest, HttpResponse, RequestBody, RequestParts, Scheme, WebSocketRequest,
};
use crate::websocket::{self, HandshakeOutcome, WebSocketHandshake};
use nitro_observability::metrics;

/// State shared by every connection a worker serves.
#[derive(Debug)]
pub struct ConnectionContext<D: Dispatch> {
    dispatch: D,
    config: Arc<ServerConfig>,
    server_drain: DisconnectSignal,
    alt_svc: Option<HeaderValue>,
    server_header: Option<HeaderValue>,
    access_log: Option<Arc<AccessLogger>>,
}

impl<D: Dispatch> Clone for ConnectionContext<D> {
    fn clone(&self) -> Self {
        Self {
            dispatch: self.dispatch.clone(),
            config: Arc::clone(&self.config),
            server_drain: self.server_drain.clone(),
            alt_svc: self.alt_svc.clone(),
            server_header: self.server_header.clone(),
            access_log: self.access_log.clone(),
        }
    }
}

impl<D: Dispatch> ConnectionContext<D> {
    pub fn new(
        dispatch: D,
        config: Arc<ServerConfig>,
        server_drain: DisconnectSignal,
        served_port: Option<u16>,
    ) -> Self {
        let alt_svc = config
            .alt_svc
            .header_value(config.http, served_port.or_else(|| config.bind.port()))
            .and_then(|value| header_value(&value, "alt-svc"));
        let server_header = config
            .server_header
            .as_deref()
            .and_then(|value| header_value(value, "server"));
        let access_log = config
            .access_log
            .as_ref()
            .map(|settings| Arc::new(AccessLogger::new(settings)));

        Self {
            dispatch,
            config,
            server_drain,
            alt_svc,
            server_header,
            access_log,
        }
    }

    /// Reuse an already-open access log rather than opening a second handle to
    /// the same file.
    pub fn with_access_logger(mut self, logger: Option<Arc<AccessLogger>>) -> Self {
        self.access_log = logger;
        self
    }

    pub fn config(&self) -> &Arc<ServerConfig> {
        &self.config
    }

    pub fn dispatch(&self) -> &D {
        &self.dispatch
    }

    /// Whether this server answers for the host the request names.
    ///
    /// The authority is read the same way [`RequestParts::authority`] reads it
    /// — from the request target where the protocol carries one, and from the
    /// `Host` header otherwise — but without building the parts, since a
    /// refused request never needs them.
    pub(crate) fn permits_host(&self, uri: &http::Uri, headers: &http::HeaderMap) -> bool {
        if self.config.allowed_hosts.is_unrestricted() {
            return true;
        }

        let authority = uri
            .authority()
            .map(|authority| authority.as_str())
            .or_else(|| {
                headers
                    .get(http::header::HOST)
                    .and_then(|value| value.to_str().ok())
            });

        if !self.config.allowed_hosts.permits(authority) {
            tracing::debug!(host = ?authority, "refusing a request for an unconfigured host");
            return false;
        }
        true
    }

    /// The headers the server owns, added to a response on its way out.
    ///
    /// Every transport calls this, because a response should not depend on
    /// which one carried it: HTTP/3 announcing no server and no length while
    /// HTTP/2 announces both would be a difference in the protocol's clothing
    /// rather than in the protocol.
    pub(crate) fn decorate(
        &self,
        headers: &mut http::HeaderMap,
        status: StatusCode,
        content_length: Option<u64>,
    ) {
        if let Some(server) = &self.server_header
            && !headers.contains_key(http::header::SERVER)
        {
            headers.insert(http::header::SERVER, server.clone());
        }
        if let Some(alt_svc) = &self.alt_svc
            && !headers.contains_key("alt-svc")
        {
            headers.insert(
                http::header::HeaderName::from_static("alt-svc"),
                alt_svc.clone(),
            );
        }
        // A body of known size gets an explicit length so responses that cannot
        // use chunked transfer encoding still frame correctly.
        if let Some(length) = content_length
            && !headers.contains_key(http::header::CONTENT_LENGTH)
            && status != StatusCode::NO_CONTENT
        {
            headers.insert(http::header::CONTENT_LENGTH, HeaderValue::from(length));
        }
        // Added here rather than left to hyper, which only covers the TCP
        // transports. Hyper skips its own when the header is already set, so
        // one date is sent either way.
        if !headers.contains_key(http::header::DATE) {
            let date = http_date();
            if !date.is_empty() {
                headers.insert(http::header::DATE, date);
            }
        }
    }

    /// The summary an access log entry is built from, or `None` when nothing
    /// is logging. Taken before the request is consumed.
    pub(crate) fn summarise(&self, parts: &RequestParts) -> Option<RequestSummary> {
        self.access_log
            .as_ref()
            .map(|_| RequestSummary::from(parts))
    }

    /// Write one access log entry, if an access log is configured.
    pub(crate) fn log_access(
        &self,
        summary: Option<RequestSummary>,
        status: u16,
        body_length: Option<u64>,
        duration: std::time::Duration,
    ) {
        if let (Some(logger), Some(summary)) = (&self.access_log, summary) {
            logger.record(AccessRecord {
                client: summary.client,
                method: &summary.method,
                target: &summary.target,
                http_version: summary.version,
                status,
                body_length,
                referer: summary.referer.as_deref(),
                user_agent: summary.user_agent.as_deref(),
                duration,
            });
        }
    }
}

/// The `Date` layout every HTTP response uses: RFC 9110's IMF-fixdate.
const HTTP_DATE: &[time::format_description::BorrowedFormatItem<'_>] = time::macros::format_description!(
    "[weekday repr:short], [day] [month repr:short] [year] [hour]:[minute]:[second] GMT"
);

/// The current time as a `Date` header, formatted at most once a second.
///
/// A second is the resolution the header itself has, so re-formatting per
/// response would produce the same bytes at a cost paid on every exchange.
fn http_date() -> HeaderValue {
    static CACHED: LazyLock<RwLock<(u64, HeaderValue)>> =
        LazyLock::new(|| RwLock::new((0, HeaderValue::from_static(""))));

    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|since| since.as_secs())
        .unwrap_or(0);

    if let Ok(cached) = CACHED.read()
        && cached.0 == now
    {
        return cached.1.clone();
    }

    let formatted = time::OffsetDateTime::from(SystemTime::now())
        .format(HTTP_DATE)
        .ok()
        .and_then(|text| HeaderValue::from_str(&text).ok())
        // A clock that cannot be formatted is no reason to fail a response; the
        // header is omitted for this second instead.
        .unwrap_or_else(|| HeaderValue::from_static(""));

    if let Ok(mut cached) = CACHED.write() {
        *cached = (now, formatted.clone());
    }
    formatted
}

fn header_value(value: &str, name: &str) -> Option<HeaderValue> {
    match HeaderValue::from_str(value) {
        Ok(parsed) => Some(parsed),
        Err(_) => {
            tracing::warn!(name, value, "ignoring an unusable configured header value");
            None
        }
    }
}

pub async fn serve_tcp<D: Dispatch>(
    stream: tokio::net::TcpStream,
    client: SocketAddr,
    server: Option<SocketAddr>,
    scheme: Scheme,
    tls: Option<TlsAcceptor>,
    context: ConnectionContext<D>,
    graceful: DisconnectSignal,
) {
    let addresses = ConnectionAddresses {
        client: Some(client),
        server,
    };

    metrics::connection_opened(metrics::Transport::Tcp);

    match tls {
        Some(acceptor) => match acceptor.accept(stream).await {
            Ok(stream) => serve_io(stream, addresses, scheme, context, graceful).await,
            Err(error) => tracing::debug!(%client, %error, "TLS handshake failed"),
        },
        None => serve_io(stream, addresses, scheme, context, graceful).await,
    }

    metrics::connection_closed(metrics::Transport::Tcp);
}

#[cfg(unix)]
pub async fn serve_unix<D: Dispatch>(
    stream: tokio::net::UnixStream,
    context: ConnectionContext<D>,
    graceful: DisconnectSignal,
) {
    let addresses = ConnectionAddresses {
        client: None,
        server: None,
    };

    metrics::connection_opened(metrics::Transport::Unix);
    serve_io(stream, addresses, Scheme::Http, context, graceful).await;
    metrics::connection_closed(metrics::Transport::Unix);
}

/// Poll a connection to completion, asking it to shut down gracefully once the
/// server starts draining.
///
/// The two connection builders produce unrelated types that happen to share an
/// inherent `graceful_shutdown` method, so the loop is generated for each
/// rather than written against a common trait.
macro_rules! drive_connection {
    ($connection:expr, $graceful:expr) => {{
        let mut connection = std::pin::pin!($connection);
        let mut shutting_down = false;
        loop {
            tokio::select! {
                result = connection.as_mut() => break result,
                () = $graceful.wait(), if !shutting_down => {
                    shutting_down = true;
                    connection.as_mut().graceful_shutdown();
                }
            }
        }
    }};
}

async fn serve_io<I, D>(
    io: I,
    addresses: ConnectionAddresses,
    scheme: Scheme,
    context: ConnectionContext<D>,
    graceful: DisconnectSignal,
) where
    I: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    D: Dispatch,
{
    // Dropping this at the end of the function is what reports the disconnect,
    // including when the connection ends by error or panic.
    let guard = DisconnectGuard::new();
    let service = RequestService {
        context: context.clone(),
        addresses,
        scheme,
        disconnect: guard.signal(),
    };

    let io = TokioIo::new(io);
    let result = if context.config.http.h2_enabled() {
        let builder = hyper_util::server::conn::auto::Builder::new(TokioExecutor::new());
        drive_connection!(
            builder.serve_connection_with_upgrades(io, service),
            graceful
        )
        .map_err(|error| error.to_string())
    } else {
        let connection = hyper::server::conn::http1::Builder::new()
            .serve_connection(io, service)
            .with_upgrades();
        drive_connection!(connection, graceful).map_err(|error| error.to_string())
    };

    if let Err(error) = result {
        tracing::debug!(error, "connection closed with an error");
    }
    drop(guard);
}

struct RequestService<D: Dispatch> {
    context: ConnectionContext<D>,
    addresses: ConnectionAddresses,
    scheme: Scheme,
    disconnect: DisconnectSignal,
}

impl<D: Dispatch> Service<Request<Incoming>> for RequestService<D> {
    type Response = Response<BoxBody<Bytes, StreamError>>;
    type Error = Infallible;
    type Future =
        std::pin::Pin<Box<dyn Future<Output = Result<Self::Response, Self::Error>> + Send>>;

    fn call(&self, mut request: Request<Incoming>) -> Self::Future {
        let context = self.context.clone();
        let addresses = self.addresses;
        let scheme = self.scheme;
        let watcher =
            DisconnectWatcher::new(self.disconnect.clone(), self.context.server_drain.clone());

        // Before anything else, including the upgrade path: a request for a
        // host this server does not answer for is refused here rather than
        // reaching the application, so nothing is built from a name a client
        // chose.
        if !context.permits_host(request.uri(), request.headers()) {
            let refusal = finish(host_refusal(), &context);
            return Box::pin(async move { Ok(refusal) });
        }

        if context.config.websockets && websocket::is_upgrade_request(request.headers()) {
            let upgrading = start_upgrade(&mut request, addresses, scheme, watcher, context);
            return Box::pin(async move { Ok(upgrading.await) });
        }

        Box::pin(async move { Ok(exchange(request, addresses, scheme, watcher, context).await) })
    }
}

/// Hand a WebSocket upgrade to the application and answer with whatever it
/// decides.
///
/// The handler runs as its own task because the upgrade only completes after
/// the acceptance has been written, which cannot happen until this function has
/// returned the response.
fn start_upgrade<D: Dispatch>(
    request: &mut Request<Incoming>,
    addresses: ConnectionAddresses,
    scheme: Scheme,
    disconnect: DisconnectWatcher,
    context: ConnectionContext<D>,
) -> impl Future<Output = Response<BoxBody<Bytes, StreamError>>> + Send + use<D> {
    let key = request
        .headers()
        .get("sec-websocket-key")
        .map(|key| key.as_bytes().to_vec())
        .unwrap_or_default();
    let subprotocols = websocket::offered_subprotocols(request.headers());

    let parts = request_parts(
        request.method().clone(),
        request.uri().clone(),
        request.version(),
        request.headers().clone(),
        scheme,
        addresses,
    );

    let (answer, outcome) = tokio::sync::oneshot::channel();
    let handshake = WebSocketHandshake::new(subprotocols, answer, hyper::upgrade::on(request));

    tokio::spawn(async move {
        context
            .dispatch
            .handle_websocket(WebSocketRequest {
                parts,
                handshake,
                disconnect,
            })
            .await;
    });

    async move {
        match outcome.await {
            Ok(HandshakeOutcome::Accepted { subprotocol }) => {
                metrics::socket_handshake(metrics::SocketProtocol::WebSocket, true);
                acceptance(&key, subprotocol)
            }
            Ok(HandshakeOutcome::Rejected { status, reason }) => {
                metrics::socket_handshake(metrics::SocketProtocol::WebSocket, false);
                finish_plain(HttpResponse::text(status, reason))
            }
            Err(_) => {
                metrics::socket_handshake(metrics::SocketProtocol::WebSocket, false);
                tracing::error!("a WebSocket handler ended without answering the handshake");
                finish_plain(HttpResponse::text(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Internal Server Error",
                ))
            }
        }
    }
}

fn acceptance(key: &[u8], subprotocol: Option<String>) -> Response<BoxBody<Bytes, StreamError>> {
    let mut response = HttpResponse::empty(StatusCode::SWITCHING_PROTOCOLS)
        .with_header("connection", "Upgrade")
        .with_header("upgrade", "websocket")
        .with_header("sec-websocket-accept", &websocket::accept_key(key));

    if let Some(subprotocol) = subprotocol {
        response = response.with_header("sec-websocket-protocol", &subprotocol);
    }
    finish_plain(response)
}

/// Build a response without the headers the server adds to ordinary
/// exchanges. An upgrade carries only what the handshake calls for.
fn finish_plain(response: HttpResponse) -> Response<BoxBody<Bytes, StreamError>> {
    let HttpResponse {
        status,
        headers,
        body,
        route: _,
    } = response;
    let length = body.content_length();
    let mut built = Response::new(body.into_boxed());
    *built.status_mut() = status;
    *built.headers_mut() = headers.into_map();

    if status != StatusCode::SWITCHING_PROTOCOLS
        && let Some(length) = length
        && !built.headers().contains_key(http::header::CONTENT_LENGTH)
    {
        built
            .headers_mut()
            .insert(http::header::CONTENT_LENGTH, HeaderValue::from(length));
    }
    built
}

/// The answer to a request naming a host this server does not serve.
///
/// Deliberately says nothing about which hosts would have worked: the list is
/// deployment configuration, not something a client is entitled to enumerate.
pub(crate) fn host_refusal() -> HttpResponse {
    HttpResponse::text(
        StatusCode::BAD_REQUEST,
        "Bad Request: unrecognised Host header",
    )
}

fn request_parts(
    method: http::Method,
    uri: http::Uri,
    version: http::Version,
    headers: http::HeaderMap,
    scheme: Scheme,
    addresses: ConnectionAddresses,
) -> RequestParts {
    RequestParts {
        method,
        uri,
        version,
        headers: Headers::from(headers),
        scheme,
        client: addresses.client,
        server: addresses.server,
    }
}

async fn exchange<D: Dispatch>(
    request: Request<Incoming>,
    addresses: ConnectionAddresses,
    scheme: Scheme,
    disconnect: DisconnectWatcher,
    context: ConnectionContext<D>,
) -> Response<BoxBody<Bytes, StreamError>> {
    let started = Instant::now();
    metrics::request_started();
    let (parts, body) = request.into_parts();
    let method = parts.method.clone();

    let request_parts = request_parts(
        parts.method,
        parts.uri,
        parts.version,
        parts.headers,
        scheme,
        addresses,
    );

    let logged = context.summarise(&request_parts);

    let answered = context
        .dispatch
        .handle_http(HttpRequest {
            parts: request_parts,
            body: RequestBody::new(body),
            disconnect,
        })
        .await;

    let route = answered.route.clone();
    let response = finish(answered, &context);

    metrics::request_finished();
    metrics::record_request(
        route.as_deref(),
        method.as_str(),
        response.status().as_u16(),
        started.elapsed(),
    );

    context.log_access(
        logged,
        response.status().as_u16(),
        response
            .headers()
            .get(http::header::CONTENT_LENGTH)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.parse().ok()),
        started.elapsed(),
    );

    response
}

/// Add the headers the server owns, then hand the response to hyper.
fn finish<D: Dispatch>(
    response: HttpResponse,
    context: &ConnectionContext<D>,
) -> Response<BoxBody<Bytes, StreamError>> {
    let HttpResponse {
        status,
        headers,
        body,
        route: _,
    } = response;

    let content_length = body.content_length();
    let mut built = Response::new(body.into_boxed());
    *built.status_mut() = status;
    *built.headers_mut() = headers.into_map();

    context.decorate(built.headers_mut(), status, content_length);
    built
}

/// The parts of a request an access log entry needs, copied before the request
/// is consumed.
pub(crate) struct RequestSummary {
    pub client: Option<SocketAddr>,
    pub method: String,
    pub target: String,
    pub version: &'static str,
    pub referer: Option<String>,
    pub user_agent: Option<String>,
}

impl From<&RequestParts> for RequestSummary {
    fn from(parts: &RequestParts) -> Self {
        Self {
            client: parts.client,
            method: parts.method.to_string(),
            target: parts
                .uri
                .path_and_query()
                .map(|target| target.as_str().to_owned())
                .unwrap_or_else(|| parts.uri.path().to_owned()),
            version: parts.http_version(),
            referer: parts.headers.get("referer").map(str::to_owned),
            user_agent: parts.headers.get("user-agent").map(str::to_owned),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{AltSvc, HttpVersion, ServerConfig};
    use crate::transport::ResponseBody;

    #[derive(Clone)]
    struct NoopDispatch;

    impl Dispatch for NoopDispatch {
        async fn handle_http(&self, _request: HttpRequest) -> HttpResponse {
            HttpResponse::empty(StatusCode::OK)
        }
    }

    fn context(config: ServerConfig) -> ConnectionContext<NoopDispatch> {
        let port = config.bind.port();
        ConnectionContext::new(
            NoopDispatch,
            Arc::new(config),
            DisconnectSignal::new(),
            port,
        )
    }

    #[test]
    fn the_server_header_is_added_when_absent() {
        let response = finish(
            HttpResponse::empty(StatusCode::OK),
            &context(ServerConfig::default()),
        );
        assert_eq!(response.headers()[http::header::SERVER], "nitro");
    }

    #[test]
    fn a_date_is_added_in_the_format_the_protocol_asks_for() {
        let response = finish(
            HttpResponse::empty(StatusCode::OK),
            &context(ServerConfig::default()),
        );
        let date = response.headers()[http::header::DATE].to_str().unwrap();

        // IMF-fixdate: `Sun, 06 Nov 1994 08:49:37 GMT`.
        assert!(date.ends_with(" GMT"), "{date}");
        assert_eq!(date.len(), 29, "{date}");
        time::OffsetDateTime::parse(date, &time::format_description::well_known::Rfc2822)
            .expect("the date must parse as an HTTP date");
    }

    #[test]
    fn an_application_date_is_left_alone() {
        let response = finish(
            HttpResponse::empty(StatusCode::OK)
                .with_header("date", "Sun, 06 Nov 1994 08:49:37 GMT"),
            &context(ServerConfig::default()),
        );
        assert_eq!(
            response.headers()[http::header::DATE],
            "Sun, 06 Nov 1994 08:49:37 GMT"
        );
    }

    #[test]
    fn the_date_is_reused_within_the_same_second() {
        assert_eq!(http_date(), http_date());
    }

    #[test]
    fn an_application_server_header_is_left_alone() {
        let response = finish(
            HttpResponse::empty(StatusCode::OK).with_header("server", "custom"),
            &context(ServerConfig::default()),
        );
        assert_eq!(response.headers()[http::header::SERVER], "custom");
    }

    #[test]
    fn the_server_header_can_be_suppressed() {
        let config = ServerConfig {
            server_header: None,
            ..Default::default()
        };
        let response = finish(HttpResponse::empty(StatusCode::OK), &context(config));
        assert!(!response.headers().contains_key(http::header::SERVER));
    }

    #[test]
    fn alt_svc_is_advertised_only_when_http3_is_active() {
        let with_h3 = ServerConfig {
            bind: crate::config::BindAddress::tcp("127.0.0.1", 4433),
            ..Default::default()
        };
        let response = finish(HttpResponse::empty(StatusCode::OK), &context(with_h3));
        assert_eq!(response.headers()["alt-svc"], "h3=\":4433\"");

        let without_h3 = ServerConfig {
            http: HttpVersion::Http2,
            ..Default::default()
        };
        let response = finish(HttpResponse::empty(StatusCode::OK), &context(without_h3));
        assert!(!response.headers().contains_key("alt-svc"));
    }

    #[test]
    fn alt_svc_can_be_overridden_verbatim() {
        let config = ServerConfig {
            alt_svc: AltSvc::Custom("h3=\":443\"; ma=86400".to_owned()),
            ..Default::default()
        };
        let response = finish(HttpResponse::empty(StatusCode::OK), &context(config));
        assert_eq!(response.headers()["alt-svc"], "h3=\":443\"; ma=86400");
    }

    #[test]
    fn a_known_body_length_is_declared() {
        let response = finish(
            HttpResponse::bytes(StatusCode::OK, Bytes::from_static(b"hello")),
            &context(ServerConfig::default()),
        );
        assert_eq!(response.headers()[http::header::CONTENT_LENGTH], "5");
    }

    #[test]
    fn a_streaming_body_declares_no_length() {
        let (_sender, body) = crate::streaming::channel(1);
        let response = finish(
            HttpResponse::new(StatusCode::OK, ResponseBody::Stream(body)),
            &context(ServerConfig::default()),
        );
        assert!(
            !response
                .headers()
                .contains_key(http::header::CONTENT_LENGTH)
        );
    }

    #[test]
    fn a_no_content_response_declares_no_length() {
        let response = finish(
            HttpResponse::empty(StatusCode::NO_CONTENT),
            &context(ServerConfig::default()),
        );
        assert!(
            !response
                .headers()
                .contains_key(http::header::CONTENT_LENGTH)
        );
    }

    #[test]
    fn a_request_summary_keeps_the_query_string() {
        let mut headers = Headers::new();
        headers.insert("user-agent", "probe/1.0").unwrap();
        let parts = RequestParts {
            method: http::Method::POST,
            uri: "/search?q=rust".parse().unwrap(),
            version: http::Version::HTTP_11,
            headers,
            scheme: Scheme::Http,
            client: None,
            server: None,
        };

        let summary = RequestSummary::from(&parts);
        assert_eq!(summary.target, "/search?q=rust");
        assert_eq!(summary.method, "POST");
        assert_eq!(summary.user_agent.as_deref(), Some("probe/1.0"));
        assert_eq!(summary.referer, None);
    }
}
