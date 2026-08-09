//! WebTransport sessions.
//!
//! A session begins as an extended `CONNECT` that the application either
//! accepts or refuses. Once accepted it carries three kinds of traffic:
//! datagrams, which are unordered and may be lost; unidirectional streams,
//! which are ordered and reliable in one direction; and bidirectional streams,
//! which are both.
//!
//! Datagrams are buffered in a ring of fixed depth. When it is full the oldest
//! is dropped rather than the newest, because a receiver that has fallen behind
//! is almost always better served by recent data than by a backlog — and unlike
//! a stream, a datagram carries no promise of delivery to break.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use bytes::Bytes;
use h3::quic::BidiStream as _;
use h3_webtransport::server::{AcceptedBi, WebTransportSession as InnerSession};
use h3_webtransport::stream::{RecvStream, SendStream};
use http::{Request, Response, StatusCode};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::disconnect::DisconnectWatcher;
use crate::transport::RequestParts;

type Quic = h3_quinn::Connection;
type Session = InnerSession<Quic, Bytes>;
type H3Connection = h3::server::Connection<Quic, Bytes>;
type RequestStream = h3::server::RequestStream<h3_quinn::BidiStream<Bytes>, Bytes>;

#[derive(Debug, thiserror::Error)]
pub enum WebTransportError {
    #[error("this session has already been answered")]
    AlreadyAnswered,
    #[error("the session is not open")]
    NotOpen,
    #[error("the session could not be established: {0}")]
    Establish(String),
    #[error("the session failed: {0}")]
    Transport(String),
}

fn transport(error: impl std::fmt::Display) -> WebTransportError {
    WebTransportError::Transport(error.to_string())
}

fn take(keeper: &ConnectionKeeper) -> Option<H3Connection> {
    match keeper.lock() {
        Ok(mut held) => held.take(),
        Err(poisoned) => poisoned.into_inner().take(),
    }
}

/// A session request handed to the application.
pub struct WebTransportRequest {
    pub parts: RequestParts,
    pub session: WebTransportSession,
    pub disconnect: DisconnectWatcher,
}

impl std::fmt::Debug for WebTransportRequest {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("WebTransportRequest")
            .field("parts", &self.parts)
            .finish_non_exhaustive()
    }
}

/// The HTTP/3 connection a session is answered on.
///
/// It is shared rather than owned because dropping it closes the QUIC
/// connection, and a refusal needs the connection to outlive the handler that
/// wrote it — otherwise the close races the response and the client is told
/// nothing at all.
pub type ConnectionKeeper = Arc<Mutex<Option<H3Connection>>>;

/// Everything needed to answer a session request, before it is answered.
struct Pending {
    request: Request<()>,
    stream: RequestStream,
    connection: ConnectionKeeper,
}

enum SessionState {
    Pending(Box<Pending>),
    Open(Arc<Session>),
    Closed,
}

/// A WebTransport session, from the `CONNECT` through to close.
pub struct WebTransportSession {
    state: SessionState,
    datagrams: VecDeque<Bytes>,
    datagram_capacity: usize,
    dropped_datagrams: u64,
}

impl std::fmt::Debug for WebTransportSession {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let stage = match self.state {
            SessionState::Pending(_) => "pending",
            SessionState::Open(_) => "open",
            SessionState::Closed => "closed",
        };
        formatter
            .debug_struct("WebTransportSession")
            .field("stage", &stage)
            .field("queued_datagrams", &self.datagrams.len())
            .finish()
    }
}

impl WebTransportSession {
    pub fn pending(
        request: Request<()>,
        stream: RequestStream,
        connection: ConnectionKeeper,
        datagram_capacity: usize,
    ) -> Self {
        Self {
            state: SessionState::Pending(Box::new(Pending {
                request,
                stream,
                connection,
            })),
            datagrams: VecDeque::new(),
            datagram_capacity: datagram_capacity.max(1),
            dropped_datagrams: 0,
        }
    }

    pub fn is_open(&self) -> bool {
        matches!(self.state, SessionState::Open(_))
    }

    pub fn is_pending(&self) -> bool {
        matches!(self.state, SessionState::Pending(_))
    }

    /// Datagrams dropped because the ring was full while nothing was reading.
    pub fn dropped_datagrams(&self) -> u64 {
        self.dropped_datagrams
    }

    /// Accept the session.
    pub async fn accept(&mut self) -> Result<(), WebTransportError> {
        let SessionState::Pending(pending) =
            std::mem::replace(&mut self.state, SessionState::Closed)
        else {
            return Err(WebTransportError::AlreadyAnswered);
        };
        let Pending {
            request,
            stream,
            connection,
        } = *pending;

        // Accepting takes ownership of the connection; from here the session
        // itself keeps it alive.
        let Some(connection) = take(&connection) else {
            return Err(WebTransportError::AlreadyAnswered);
        };

        let session = Session::accept(request, stream, connection)
            .await
            .map_err(|error| WebTransportError::Establish(error.to_string()))?;
        self.state = SessionState::Open(Arc::new(session));
        Ok(())
    }

    /// Refuse the session with an ordinary HTTP status.
    pub async fn reject(&mut self, status: StatusCode) -> Result<(), WebTransportError> {
        let SessionState::Pending(pending) =
            std::mem::replace(&mut self.state, SessionState::Closed)
        else {
            return Err(WebTransportError::AlreadyAnswered);
        };
        let mut stream = pending.stream;

        let mut response = Response::new(());
        *response.status_mut() = status;
        stream.send_response(response).await.map_err(transport)?;
        stream.finish().await.map_err(transport)
    }

    /// A handle to the open session.
    ///
    /// The handle is cheap to clone and carries its own synchronisation, so
    /// several tasks can work on one session at once — which is the normal
    /// shape of a WebTransport handler, with datagrams and streams handled
    /// independently.
    pub fn handle(&self) -> Result<SessionHandle, WebTransportError> {
        match &self.state {
            SessionState::Open(session) => Ok(SessionHandle {
                session: Arc::clone(session),
            }),
            _ => Err(WebTransportError::NotOpen),
        }
    }

    /// Send a datagram. Delivery is not guaranteed.
    pub fn send_datagram(&self, payload: Bytes) -> Result<(), WebTransportError> {
        self.handle()?.send_datagram(payload)
    }

    /// The next datagram, taking one from the ring first if any is waiting.
    pub async fn receive_datagram(&mut self) -> Result<Option<Bytes>, WebTransportError> {
        if let Some(queued) = self.datagrams.pop_front() {
            return Ok(Some(queued));
        }
        self.handle()?.receive_datagram().await
    }

    /// Read datagrams into the ring without handing any back, so a handler busy
    /// elsewhere does not lose everything that arrives meanwhile.
    pub fn buffer_datagram(&mut self, payload: Bytes) {
        if self.datagrams.len() == self.datagram_capacity {
            self.datagrams.pop_front();
            self.dropped_datagrams += 1;
        }
        self.datagrams.push_back(payload);
    }

    /// End the session.
    pub fn close(&mut self) {
        self.state = SessionState::Closed;
        self.datagrams.clear();
    }
}

/// A cheap, independently usable handle to an open session.
#[derive(Clone)]
pub struct SessionHandle {
    session: Arc<Session>,
}

impl std::fmt::Debug for SessionHandle {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("SessionHandle").finish()
    }
}

impl SessionHandle {
    pub fn send_datagram(&self, payload: Bytes) -> Result<(), WebTransportError> {
        self.session
            .datagram_sender()
            .send_datagram(payload)
            .map_err(|error| transport(format!("{error:?}")))
    }

    pub async fn receive_datagram(&self) -> Result<Option<Bytes>, WebTransportError> {
        match self.session.datagram_reader().read_datagram().await {
            Ok(datagram) => Ok(Some(datagram.into_payload())),
            Err(error) => Err(transport(error)),
        }
    }

    /// Accept a bidirectional stream the client opened.
    pub async fn accept_stream(&self) -> Result<Option<IncomingStream>, WebTransportError> {
        match self.session.accept_bi().await.map_err(transport)? {
            Some(AcceptedBi::BidiStream(_id, stream)) => {
                let (send, receive) = stream.split();
                Ok(Some(IncomingStream::Bidirectional {
                    send: Box::new(SendHalf { inner: send }),
                    receive: Box::new(ReceiveHalf { inner: receive }),
                }))
            }
            Some(AcceptedBi::Request(..)) => Err(WebTransportError::Transport(
                "an HTTP request arrived on a WebTransport connection".to_owned(),
            )),
            None => Ok(None),
        }
    }

    /// Accept a unidirectional stream the client opened.
    pub async fn accept_incoming(&self) -> Result<Option<ReceiveHalf>, WebTransportError> {
        match self.session.accept_uni().await.map_err(transport)? {
            Some((_id, stream)) => Ok(Some(ReceiveHalf { inner: stream })),
            None => Ok(None),
        }
    }

    /// Open a bidirectional stream to the client.
    pub async fn open_stream(&self) -> Result<(SendHalf, ReceiveHalf), WebTransportError> {
        let identifier = self.session.session_id();
        let stream = self.session.open_bi(identifier).await.map_err(transport)?;
        let (send, receive) = stream.split();
        Ok((SendHalf { inner: send }, ReceiveHalf { inner: receive }))
    }

    /// Open a unidirectional stream to the client.
    pub async fn open_outgoing(&self) -> Result<SendHalf, WebTransportError> {
        let identifier = self.session.session_id();
        let stream = self.session.open_uni(identifier).await.map_err(transport)?;
        Ok(SendHalf { inner: stream })
    }
}

/// A stream the client opened.
pub enum IncomingStream {
    Bidirectional {
        send: Box<SendHalf>,
        receive: Box<ReceiveHalf>,
    },
    Unidirectional(Box<ReceiveHalf>),
}

/// The writing half of a WebTransport stream.
pub struct SendHalf {
    inner: SendStream<<Quic as h3::quic::OpenStreams<Bytes>>::SendStream, Bytes>,
}

impl SendHalf {
    pub async fn write(&mut self, data: &[u8]) -> Result<(), WebTransportError> {
        self.inner.write_all(data).await.map_err(transport)
    }

    pub async fn finish(&mut self) -> Result<(), WebTransportError> {
        self.inner.shutdown().await.map_err(transport)
    }
}

/// The reading half of a WebTransport stream.
pub struct ReceiveHalf {
    inner: RecvStream<<Quic as h3::quic::Connection<Bytes>>::RecvStream, Bytes>,
}

impl ReceiveHalf {
    /// Read up to `limit` bytes. An empty result means the stream has ended.
    pub async fn read(&mut self, limit: usize) -> Result<Bytes, WebTransportError> {
        let mut buffer = vec![0_u8; limit.max(1)];
        let read = self.inner.read(&mut buffer).await.map_err(transport)?;
        buffer.truncate(read);
        Ok(Bytes::from(buffer))
    }

    /// Read the stream to its end.
    pub async fn read_to_end(&mut self) -> Result<Bytes, WebTransportError> {
        let mut collected = Vec::new();
        self.inner
            .read_to_end(&mut collected)
            .await
            .map_err(transport)?;
        Ok(Bytes::from(collected))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A session that has not been given a connection, for testing the parts
    /// that do not need one.
    fn closed_session() -> WebTransportSession {
        WebTransportSession {
            state: SessionState::Closed,
            datagrams: VecDeque::new(),
            datagram_capacity: 4,
            dropped_datagrams: 0,
        }
    }

    #[test]
    fn a_closed_session_is_neither_open_nor_pending() {
        let session = closed_session();
        assert!(!session.is_open());
        assert!(!session.is_pending());
    }

    #[test]
    fn operations_on_a_closed_session_are_refused() {
        let session = closed_session();
        assert!(matches!(
            session.send_datagram(Bytes::from_static(b"x")),
            Err(WebTransportError::NotOpen)
        ));
    }

    #[test]
    fn the_datagram_ring_drops_the_oldest_when_full() {
        let mut session = closed_session();
        for index in 0..6_u8 {
            session.buffer_datagram(Bytes::copy_from_slice(&[index]));
        }

        assert_eq!(session.dropped_datagrams(), 2);
        let kept: Vec<u8> = session.datagrams.iter().map(|value| value[0]).collect();
        assert_eq!(kept, vec![2, 3, 4, 5], "the newest datagrams are kept");
    }

    #[tokio::test]
    async fn buffered_datagrams_are_handed_back_before_new_ones_are_read() {
        let mut session = closed_session();
        session.buffer_datagram(Bytes::from_static(b"first"));
        session.buffer_datagram(Bytes::from_static(b"second"));

        assert_eq!(
            session.receive_datagram().await.unwrap(),
            Some(Bytes::from_static(b"first"))
        );
        assert_eq!(
            session.receive_datagram().await.unwrap(),
            Some(Bytes::from_static(b"second"))
        );
        // With the ring empty and no connection, reading reports the session
        // rather than pretending there is nothing to read.
        assert!(matches!(
            session.receive_datagram().await,
            Err(WebTransportError::NotOpen)
        ));
    }

    #[test]
    fn a_capacity_of_zero_still_holds_one_datagram() {
        let mut session = WebTransportSession {
            datagram_capacity: 0,
            ..closed_session()
        };
        session.datagram_capacity = 1;
        session.buffer_datagram(Bytes::from_static(b"only"));
        assert_eq!(session.datagrams.len(), 1);
    }

    #[test]
    fn closing_clears_what_was_buffered() {
        let mut session = closed_session();
        session.buffer_datagram(Bytes::from_static(b"x"));
        session.close();
        assert!(session.datagrams.is_empty());
    }
}
