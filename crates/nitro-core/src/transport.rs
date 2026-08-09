//! Socket binding, accept loops and per-connection serving.
//!
//! Nothing below this module knows how a request is answered. The accept and
//! connection code is generic over [`Dispatch`], and the single implementation
//! of that trait lives in the binding crate. That is what keeps the whole
//! transport path testable with an ordinary Rust closure standing in for an
//! application.

pub mod accept;
pub mod connection;
pub mod quic;
pub mod tls;

use std::future::Future;
use std::net::SocketAddr;

use bytes::Bytes;
use http::{Method, StatusCode, Uri, Version};
use http_body_util::{BodyExt, Empty, Full, combinators::BoxBody};

use crate::disconnect::DisconnectWatcher;
use crate::files::FileBody;
use crate::headers::Headers;
use crate::streaming::{StreamBody, StreamError};
use crate::websocket::WebSocketHandshake;
use crate::webtransport::WebTransportRequest;

/// The URI scheme a request arrived under.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scheme {
    Http,
    Https,
}

impl Scheme {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Http => "http",
            Self::Https => "https",
        }
    }

    pub fn is_secure(self) -> bool {
        matches!(self, Self::Https)
    }
}

/// Everything known about a request before its body is read.
#[derive(Debug, Clone)]
pub struct RequestParts {
    pub method: Method,
    pub uri: Uri,
    pub version: Version,
    pub headers: Headers,
    pub scheme: Scheme,
    pub client: Option<SocketAddr>,
    pub server: Option<SocketAddr>,
}

impl RequestParts {
    pub fn path(&self) -> &str {
        self.uri.path()
    }

    pub fn query(&self) -> &str {
        self.uri.query().unwrap_or("")
    }

    /// The host the client addressed, from the request target on HTTP/2 and
    /// HTTP/3, falling back to the `Host` header on HTTP/1.1.
    pub fn authority(&self) -> Option<&str> {
        self.uri
            .authority()
            .map(|authority| authority.as_str())
            .or_else(|| self.headers.get("host"))
    }

    pub fn http_version(&self) -> &'static str {
        match self.version {
            Version::HTTP_09 => "0.9",
            Version::HTTP_10 => "1.0",
            Version::HTTP_11 => "1.1",
            Version::HTTP_2 => "2",
            Version::HTTP_3 => "3",
            _ => "1.1",
        }
    }
}

#[derive(Debug, thiserror::Error)]
#[error("reading the request body failed: {0}")]
pub struct BodyError(String);

impl BodyError {
    pub fn new(reason: impl std::fmt::Display) -> Self {
        Self(reason.to_string())
    }
}

/// Request body, read on demand rather than buffered up front.
///
/// The two forms exist because the two transports deliver a body differently:
/// over TCP it arrives as an HTTP body, and over QUIC it is read off a stream
/// by the task that owns the connection and handed across a channel. Handlers
/// see no difference.
#[derive(Debug)]
pub enum RequestBody {
    Http(hyper::body::Incoming),
    Chunks(tokio::sync::mpsc::Receiver<Result<Bytes, BodyError>>),
    Empty,
}

impl RequestBody {
    pub fn new(inner: hyper::body::Incoming) -> Self {
        Self::Http(inner)
    }

    /// Read the next chunk of data, skipping trailer frames.
    pub async fn next_chunk(&mut self) -> Option<Result<Bytes, BodyError>> {
        match self {
            Self::Http(incoming) => loop {
                match incoming.frame().await? {
                    Ok(frame) => match frame.into_data() {
                        Ok(chunk) => return Some(Ok(chunk)),
                        Err(_trailers) => continue,
                    },
                    Err(error) => return Some(Err(BodyError::new(error))),
                }
            },
            Self::Chunks(receiver) => receiver.recv().await,
            Self::Empty => None,
        }
    }

    /// Read the body to completion.
    pub async fn collect(mut self) -> Result<Bytes, BodyError> {
        if let Self::Http(incoming) = self {
            return incoming
                .collect()
                .await
                .map(|collected| collected.to_bytes())
                .map_err(BodyError::new);
        }

        let mut collected = bytes::BytesMut::new();
        while let Some(chunk) = self.next_chunk().await {
            collected.extend_from_slice(&chunk?);
        }
        Ok(collected.freeze())
    }
}

/// A request handed to [`Dispatch::handle_http`].
#[derive(Debug)]
pub struct HttpRequest {
    pub parts: RequestParts,
    pub body: RequestBody,
    pub disconnect: DisconnectWatcher,
}

/// The payload of a response.
#[derive(Debug)]
pub enum ResponseBody {
    Empty,
    Bytes(Bytes),
    Stream(StreamBody),
    File(FileBody),
}

impl ResponseBody {
    /// The byte length, when it is known before sending.
    pub fn content_length(&self) -> Option<u64> {
        match self {
            Self::Empty => Some(0),
            Self::Bytes(bytes) => Some(bytes.len() as u64),
            Self::File(body) => Some(body.remaining()),
            Self::Stream(_) => None,
        }
    }

    pub(crate) fn into_boxed(self) -> BoxBody<Bytes, StreamError> {
        match self {
            Self::Empty => Empty::new().map_err(|never| match never {}).boxed(),
            Self::Bytes(bytes) => Full::new(bytes).map_err(|never| match never {}).boxed(),
            Self::Stream(body) => body.boxed(),
            Self::File(body) => body.boxed(),
        }
    }
}

/// A response produced by [`Dispatch::handle_http`].
#[derive(Debug)]
pub struct HttpResponse {
    pub status: StatusCode,
    pub headers: Headers,
    pub body: ResponseBody,
    /// The route pattern that produced this response, for metric labels.
    ///
    /// It travels back with the response because only the dispatcher knows it:
    /// the transport hands a request over without having matched anything. It
    /// is never sent to the client, and the pattern is used rather than the
    /// path so that a route with an identifier in it stays one time series
    /// instead of one per identifier.
    pub route: Option<String>,
}

impl HttpResponse {
    pub fn new(status: StatusCode, body: ResponseBody) -> Self {
        Self {
            status,
            headers: Headers::new(),
            body,
            route: None,
        }
    }

    /// Label this response with the route that produced it.
    pub fn from_route(mut self, route: Option<String>) -> Self {
        self.route = route;
        self
    }

    pub fn empty(status: StatusCode) -> Self {
        Self::new(status, ResponseBody::Empty)
    }

    pub fn bytes(status: StatusCode, body: impl Into<Bytes>) -> Self {
        Self::new(status, ResponseBody::Bytes(body.into()))
    }

    /// A plain-text response, used for errors the transport produces itself
    /// when the application could not.
    pub fn text(status: StatusCode, body: impl Into<Bytes>) -> Self {
        let mut response = Self::bytes(status, body);
        response
            .headers
            .insert("content-type", "text/plain; charset=utf-8")
            .expect("a literal content type is always valid");
        response
    }

    pub fn with_header(mut self, name: &str, value: &str) -> Self {
        if let Err(error) = self.headers.insert(name, value) {
            tracing::warn!(%error, "dropping an invalid response header");
        }
        self
    }
}

/// A request asking to become a WebSocket connection.
///
/// The handshake is still open: answering it is the handler's job, and until it
/// does the client has had no response at all.
#[derive(Debug)]
pub struct WebSocketRequest {
    pub parts: RequestParts,
    pub handshake: WebSocketHandshake,
    pub disconnect: DisconnectWatcher,
}

/// The bridge between the transport and whatever answers requests.
///
/// Implementations are cloned per connection, so cloning must be cheap.
pub trait Dispatch: Clone + Send + Sync + 'static {
    fn handle_http(&self, request: HttpRequest) -> impl Future<Output = HttpResponse> + Send;

    /// Answer a WebSocket upgrade. The default refuses every upgrade, which is
    /// the right behaviour for an application that does not speak WebSocket.
    fn handle_websocket(&self, request: WebSocketRequest) -> impl Future<Output = ()> + Send {
        async move {
            let mut request = request;
            if let Err(error) = request.handshake.reject(
                StatusCode::NOT_IMPLEMENTED,
                "WebSocket is not supported here",
            ) {
                tracing::debug!(%error, "could not refuse a WebSocket upgrade");
            }
        }
    }

    /// Answer a WebTransport session request. The default refuses every one.
    fn handle_webtransport(&self, request: WebTransportRequest) -> impl Future<Output = ()> + Send {
        async move {
            let mut request = request;
            if let Err(error) = request.session.reject(StatusCode::NOT_IMPLEMENTED).await {
                tracing::debug!(%error, "could not refuse a WebTransport session");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use http_body_util::BodyExt;

    use super::*;

    #[test]
    fn content_length_is_known_for_everything_but_a_stream() {
        assert_eq!(ResponseBody::Empty.content_length(), Some(0));
        assert_eq!(
            ResponseBody::Bytes(Bytes::from_static(b"hello")).content_length(),
            Some(5)
        );
        let (_sender, body) = crate::streaming::channel(1);
        assert_eq!(ResponseBody::Stream(body).content_length(), None);
    }

    #[tokio::test]
    async fn boxing_preserves_the_payload() {
        let collected = ResponseBody::Bytes(Bytes::from_static(b"hello"))
            .into_boxed()
            .collect()
            .await
            .expect("a byte body cannot fail")
            .to_bytes();
        assert_eq!(&collected[..], b"hello");
    }

    #[test]
    fn authority_falls_back_to_the_host_header() {
        let mut headers = Headers::new();
        headers.insert("host", "example.test:8443").unwrap();
        let parts = RequestParts {
            method: Method::GET,
            uri: "/index.html".parse().unwrap(),
            version: Version::HTTP_11,
            headers,
            scheme: Scheme::Https,
            client: None,
            server: None,
        };

        assert_eq!(parts.authority(), Some("example.test:8443"));
        assert_eq!(parts.path(), "/index.html");
        assert_eq!(parts.query(), "");
        assert_eq!(parts.http_version(), "1.1");
    }

    #[test]
    fn an_absolute_target_wins_over_the_host_header() {
        let mut headers = Headers::new();
        headers.insert("host", "stale.test").unwrap();
        let parts = RequestParts {
            method: Method::GET,
            uri: "https://authoritative.test/path?a=1".parse().unwrap(),
            version: Version::HTTP_2,
            headers,
            scheme: Scheme::Https,
            client: None,
            server: None,
        };

        assert_eq!(parts.authority(), Some("authoritative.test"));
        assert_eq!(parts.query(), "a=1");
        assert_eq!(parts.http_version(), "2");
    }
}
