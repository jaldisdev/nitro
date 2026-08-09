//! Per-connection serving.
//!
//! One connection carries one disconnect guard and any number of exchanges. The
//! guard is dropped when the connection ends for any reason, which is what
//! releases handlers still waiting on it.

use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Instant;

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
use crate::transport::{Dispatch, HttpRequest, HttpResponse, RequestBody, RequestParts, Scheme};

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
    pub fn new(dispatch: D, config: Arc<ServerConfig>, server_drain: DisconnectSignal) -> Self {
        let alt_svc = config
            .alt_svc
            .header_value(config.http, &config.bind)
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

    match tls {
        Some(acceptor) => match acceptor.accept(stream).await {
            Ok(stream) => serve_io(stream, addresses, scheme, context, graceful).await,
            Err(error) => tracing::debug!(%client, %error, "TLS handshake failed"),
        },
        None => serve_io(stream, addresses, scheme, context, graceful).await,
    }
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
    serve_io(stream, addresses, Scheme::Http, context, graceful).await;
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

    fn call(&self, request: Request<Incoming>) -> Self::Future {
        let context = self.context.clone();
        let addresses = self.addresses;
        let scheme = self.scheme;
        let watcher =
            DisconnectWatcher::new(self.disconnect.clone(), self.context.server_drain.clone());

        Box::pin(async move { Ok(exchange(request, addresses, scheme, watcher, context).await) })
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
    let (parts, body) = request.into_parts();

    let request_parts = RequestParts {
        method: parts.method,
        uri: parts.uri,
        version: parts.version,
        headers: Headers::from(parts.headers),
        scheme,
        client: addresses.client,
        server: addresses.server,
    };

    let logged = context
        .access_log
        .as_ref()
        .map(|_| RequestSummary::from(&request_parts));

    let response = context
        .dispatch
        .handle_http(HttpRequest {
            parts: request_parts,
            body: RequestBody::new(body),
            disconnect,
        })
        .await;

    let response = finish(response, &context);

    if let (Some(logger), Some(summary)) = (&context.access_log, logged) {
        logger.record(AccessRecord {
            client: summary.client,
            method: &summary.method,
            target: &summary.target,
            http_version: summary.version,
            status: response.status().as_u16(),
            body_length: response
                .headers()
                .get(http::header::CONTENT_LENGTH)
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.parse().ok()),
            referer: summary.referer.as_deref(),
            user_agent: summary.user_agent.as_deref(),
            duration: started.elapsed(),
        });
    }

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
    } = response;

    let content_length = body.content_length();
    let mut built = Response::new(body.into_boxed());
    *built.status_mut() = status;
    *built.headers_mut() = headers.into_map();

    let headers = built.headers_mut();
    if let Some(server) = &context.server_header
        && !headers.contains_key(http::header::SERVER)
    {
        headers.insert(http::header::SERVER, server.clone());
    }
    if let Some(alt_svc) = &context.alt_svc
        && !headers.contains_key("alt-svc")
    {
        headers.insert(
            http::header::HeaderName::from_static("alt-svc"),
            alt_svc.clone(),
        );
    }
    // A body of known size gets an explicit length so responses that cannot use
    // chunked transfer encoding still frame correctly.
    if let Some(length) = content_length
        && !headers.contains_key(http::header::CONTENT_LENGTH)
        && status != StatusCode::NO_CONTENT
    {
        headers.insert(http::header::CONTENT_LENGTH, HeaderValue::from(length));
    }

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
        ConnectionContext::new(NoopDispatch, Arc::new(config), DisconnectSignal::new())
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
