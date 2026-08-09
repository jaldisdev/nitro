//! WebSocket upgrade and framing.
//!
//! The application decides whether an upgrade happens. A request that looks
//! like an upgrade is handed to the handler with the handshake still open; the
//! handler either accepts it, optionally choosing a subprotocol, or rejects it
//! with an ordinary HTTP response. Only once it accepts is `101` sent, and only
//! then does the connection become a stream of messages.

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use bytes::Bytes;
use futures_util::{SinkExt, StreamExt};
use http::{HeaderMap, StatusCode};
use hyper_util::rt::TokioIo;
use sha1::{Digest, Sha1};
use tokio::sync::oneshot;
use tokio_tungstenite::WebSocketStream;
use tokio_tungstenite::tungstenite::protocol::{CloseFrame, Message, Role};

/// The constant every WebSocket handshake mixes into the client's key. It is
/// fixed by the protocol.
const HANDSHAKE_GUID: &[u8] = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

#[derive(Debug, thiserror::Error)]
pub enum WebSocketError {
    #[error("this handshake has already been answered")]
    AlreadyAnswered,
    #[error("the subprotocol {requested:?} was not offered by the client")]
    SubprotocolNotOffered { requested: String },
    #[error("the connection went away before the handshake completed")]
    HandshakeAbandoned,
    #[error("the upgrade failed: {0}")]
    Upgrade(String),
    #[error("the connection is closed")]
    Closed,
    #[error("the connection failed: {0}")]
    Transport(String),
}

/// A message crossing a WebSocket connection.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WebSocketMessage {
    Text(String),
    Binary(Bytes),
    Ping(Bytes),
    Pong(Bytes),
    Close { code: Option<u16>, reason: String },
}

impl WebSocketMessage {
    fn into_frame(self) -> Message {
        match self {
            Self::Text(text) => Message::Text(text.into()),
            Self::Binary(data) => Message::Binary(data),
            Self::Ping(data) => Message::Ping(data),
            Self::Pong(data) => Message::Pong(data),
            Self::Close { code, reason } => Message::Close(code.map(|code| CloseFrame {
                code: code.into(),
                reason: reason.into(),
            })),
        }
    }

    fn from_frame(frame: Message) -> Option<Self> {
        match frame {
            Message::Text(text) => Some(Self::Text(text.to_string())),
            Message::Binary(data) => Some(Self::Binary(data)),
            Message::Ping(data) => Some(Self::Ping(data)),
            Message::Pong(data) => Some(Self::Pong(data)),
            Message::Close(frame) => Some(Self::Close {
                code: frame.as_ref().map(|frame| frame.code.into()),
                reason: frame
                    .map(|frame| frame.reason.to_string())
                    .unwrap_or_default(),
            }),
            // Raw frames are an escape hatch of the framing library that the
            // read side never produces.
            Message::Frame(_) => None,
        }
    }
}

/// How a handshake was answered.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HandshakeOutcome {
    Accepted { subprotocol: Option<String> },
    Rejected { status: StatusCode, reason: Bytes },
}

/// Whether a request is asking to become a WebSocket connection.
pub fn is_upgrade_request(headers: &HeaderMap) -> bool {
    let names_websocket = headers
        .get(http::header::UPGRADE)
        .is_some_and(|value| value.as_bytes().eq_ignore_ascii_case(b"websocket"));

    // `Connection` is a comma-separated list, and `Upgrade` may sit anywhere in
    // it — proxies routinely add tokens of their own.
    let asks_to_upgrade = headers.get(http::header::CONNECTION).is_some_and(|value| {
        value.to_str().is_ok_and(|value| {
            value
                .split(',')
                .any(|token| token.trim().eq_ignore_ascii_case("upgrade"))
        })
    });

    names_websocket && asks_to_upgrade && headers.contains_key("sec-websocket-key")
}

/// The `Sec-WebSocket-Accept` value that answers `key`.
pub fn accept_key(key: &[u8]) -> String {
    let mut digest = Sha1::new();
    digest.update(key);
    digest.update(HANDSHAKE_GUID);
    STANDARD.encode(digest.finalize())
}

/// The subprotocols a client offered, in the order it prefers them.
pub fn offered_subprotocols(headers: &HeaderMap) -> Vec<String> {
    headers
        .get("sec-websocket-protocol")
        .and_then(|value| value.to_str().ok())
        .map(|value| {
            value
                .split(',')
                .map(|name| name.trim().to_owned())
                .filter(|name| !name.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

/// An open handshake, handed to the application to answer.
#[derive(Debug)]
pub struct WebSocketHandshake {
    subprotocols: Vec<String>,
    answer: Option<oneshot::Sender<HandshakeOutcome>>,
    upgrade: Option<hyper::upgrade::OnUpgrade>,
}

impl WebSocketHandshake {
    pub fn new(
        subprotocols: Vec<String>,
        answer: oneshot::Sender<HandshakeOutcome>,
        upgrade: hyper::upgrade::OnUpgrade,
    ) -> Self {
        Self {
            subprotocols,
            answer: Some(answer),
            upgrade: Some(upgrade),
        }
    }

    /// The subprotocols the client offered.
    pub fn subprotocols(&self) -> &[String] {
        &self.subprotocols
    }

    /// Whether the handshake is still open.
    pub fn is_open(&self) -> bool {
        self.answer.is_some()
    }

    /// Accept the upgrade and wait for the connection to become one.
    ///
    /// A subprotocol may only be chosen from those the client offered; naming
    /// one it did not offer would leave the two sides disagreeing about what
    /// they are speaking.
    pub async fn accept(
        &mut self,
        subprotocol: Option<String>,
    ) -> Result<WebSocketConnection, WebSocketError> {
        if let Some(requested) = &subprotocol
            && !self.subprotocols.contains(requested)
        {
            return Err(WebSocketError::SubprotocolNotOffered {
                requested: requested.clone(),
            });
        }

        let (answer, upgrade) = self.take()?;
        answer
            .send(HandshakeOutcome::Accepted { subprotocol })
            .map_err(|_| WebSocketError::HandshakeAbandoned)?;

        // Resolves once the acceptance has been written and the socket is no
        // longer carrying HTTP.
        let upgraded = upgrade
            .await
            .map_err(|error| WebSocketError::Upgrade(error.to_string()))?;

        let stream =
            WebSocketStream::from_raw_socket(TokioIo::new(upgraded), Role::Server, None).await;
        Ok(WebSocketConnection { stream })
    }

    /// Refuse the upgrade and answer with an ordinary HTTP response instead.
    pub fn reject(
        &mut self,
        status: StatusCode,
        reason: impl Into<Bytes>,
    ) -> Result<(), WebSocketError> {
        let (answer, _upgrade) = self.take()?;
        answer
            .send(HandshakeOutcome::Rejected {
                status,
                reason: reason.into(),
            })
            .map_err(|_| WebSocketError::HandshakeAbandoned)
    }

    fn take(
        &mut self,
    ) -> Result<(oneshot::Sender<HandshakeOutcome>, hyper::upgrade::OnUpgrade), WebSocketError>
    {
        match (self.answer.take(), self.upgrade.take()) {
            (Some(answer), Some(upgrade)) => Ok((answer, upgrade)),
            _ => Err(WebSocketError::AlreadyAnswered),
        }
    }
}

impl Drop for WebSocketHandshake {
    fn drop(&mut self) {
        // A handler that returns without answering leaves the client waiting on
        // a response that will never come, so answer for it.
        if let Some(answer) = self.answer.take() {
            let _outcome = answer.send(HandshakeOutcome::Rejected {
                status: StatusCode::INTERNAL_SERVER_ERROR,
                reason: Bytes::from_static(b"the handler did not answer the handshake"),
            });
            tracing::warn!("a WebSocket handler returned without accepting or rejecting");
        }
    }
}

/// An established WebSocket connection.
#[derive(Debug)]
pub struct WebSocketConnection {
    stream: WebSocketStream<TokioIo<hyper::upgrade::Upgraded>>,
}

impl WebSocketConnection {
    /// The next message, or `None` once the connection has ended.
    pub async fn receive(&mut self) -> Option<Result<WebSocketMessage, WebSocketError>> {
        loop {
            match self.stream.next().await? {
                Ok(frame) => match WebSocketMessage::from_frame(frame) {
                    Some(message) => return Some(Ok(message)),
                    None => continue,
                },
                Err(error) => return Some(Err(transport_error(error))),
            }
        }
    }

    pub async fn send(&mut self, message: WebSocketMessage) -> Result<(), WebSocketError> {
        self.stream
            .send(message.into_frame())
            .await
            .map_err(transport_error)
    }

    /// Close the connection, telling the peer why.
    pub async fn close(
        &mut self,
        code: Option<u16>,
        reason: impl Into<String>,
    ) -> Result<(), WebSocketError> {
        let frame = code.map(|code| CloseFrame {
            code: code.into(),
            reason: reason.into().into(),
        });
        self.stream.close(frame).await.map_err(transport_error)
    }
}

fn transport_error(error: tokio_tungstenite::tungstenite::Error) -> WebSocketError {
    use tokio_tungstenite::tungstenite::Error;

    match error {
        Error::ConnectionClosed | Error::AlreadyClosed => WebSocketError::Closed,
        other => WebSocketError::Transport(other.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use http::HeaderValue;

    use super::*;

    fn upgrade_headers() -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert(http::header::UPGRADE, HeaderValue::from_static("websocket"));
        headers.insert(
            http::header::CONNECTION,
            HeaderValue::from_static("Upgrade"),
        );
        headers.insert(
            "sec-websocket-key",
            HeaderValue::from_static("dGhlIHNhbXBsZSBub25jZQ=="),
        );
        headers
    }

    #[test]
    fn a_complete_upgrade_request_is_recognised() {
        assert!(is_upgrade_request(&upgrade_headers()));
    }

    #[test]
    fn the_upgrade_token_may_sit_among_others() {
        let mut headers = upgrade_headers();
        headers.insert(
            http::header::CONNECTION,
            HeaderValue::from_static("keep-alive, Upgrade"),
        );
        assert!(is_upgrade_request(&headers));
    }

    #[test]
    fn the_headers_are_matched_without_regard_to_case() {
        let mut headers = upgrade_headers();
        headers.insert(http::header::UPGRADE, HeaderValue::from_static("WebSocket"));
        headers.insert(
            http::header::CONNECTION,
            HeaderValue::from_static("upgrade"),
        );
        assert!(is_upgrade_request(&headers));
    }

    #[test]
    fn an_incomplete_request_is_not_an_upgrade() {
        for missing in [
            http::header::UPGRADE.as_str(),
            http::header::CONNECTION.as_str(),
            "sec-websocket-key",
        ] {
            let mut headers = upgrade_headers();
            headers.remove(missing);
            assert!(
                !is_upgrade_request(&headers),
                "a request without {missing} is not an upgrade"
            );
        }
    }

    #[test]
    fn an_ordinary_request_is_not_an_upgrade() {
        assert!(!is_upgrade_request(&HeaderMap::new()));
    }

    #[test]
    fn the_accept_key_follows_the_protocol() {
        // The example pair given by the WebSocket specification.
        assert_eq!(
            accept_key(b"dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        );
    }

    #[test]
    fn offered_subprotocols_are_split_and_trimmed() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "sec-websocket-protocol",
            HeaderValue::from_static("chat, superchat , "),
        );
        assert_eq!(offered_subprotocols(&headers), vec!["chat", "superchat"]);
    }

    #[test]
    fn no_subprotocol_header_offers_nothing() {
        assert_eq!(
            offered_subprotocols(&HeaderMap::new()),
            Vec::<String>::new()
        );
    }

    #[test]
    fn messages_survive_a_round_trip_through_frames() {
        for message in [
            WebSocketMessage::Text("hello".to_owned()),
            WebSocketMessage::Binary(Bytes::from_static(b"\x00\x01")),
            WebSocketMessage::Ping(Bytes::from_static(b"p")),
            WebSocketMessage::Pong(Bytes::from_static(b"p")),
            WebSocketMessage::Close {
                code: Some(1000),
                reason: "done".to_owned(),
            },
        ] {
            let restored = WebSocketMessage::from_frame(message.clone().into_frame());
            assert_eq!(restored, Some(message));
        }
    }

    #[test]
    fn a_close_without_a_frame_reads_as_a_bare_close() {
        assert_eq!(
            WebSocketMessage::from_frame(Message::Close(None)),
            Some(WebSocketMessage::Close {
                code: None,
                reason: String::new()
            })
        );
    }
}
