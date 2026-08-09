//! End-to-end WebTransport tests against a real client.
//!
//! These run a real server over QUIC and drive it with an independent
//! WebTransport implementation, so the accept path, datagrams and streams are
//! exercised over the wire rather than against a session built by hand.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use bytes::Bytes;
use http::StatusCode;
use nitro_core::config::{BindAddress, HttpVersion, ServerConfig, TlsSettings};
use nitro_core::lifecycle::drain::DrainCoordinator;
use nitro_core::lifecycle::signals::ShutdownController;
use nitro_core::transport::tls::TlsMaterial;
use nitro_core::transport::{Dispatch, HttpRequest, HttpResponse, accept};
use nitro_core::webtransport::{IncomingStream, WebTransportRequest};
use tokio::io::AsyncReadExt;

/// How long a client waits for something the server should send promptly.
const PATIENCE: Duration = Duration::from_secs(10);

/// Accepts every session and answers whatever arrives.
#[derive(Clone)]
struct EchoSession {
    accepted: Arc<AtomicUsize>,
}

impl Dispatch for EchoSession {
    async fn handle_http(&self, _request: HttpRequest) -> HttpResponse {
        HttpResponse::text(StatusCode::OK, "plain")
    }

    async fn handle_webtransport(&self, request: WebTransportRequest) {
        let mut request = request;
        if let Err(error) = request.session.accept().await {
            eprintln!("accepting the session failed: {error}");
            return;
        }
        self.accepted.fetch_add(1, Ordering::Release);

        let Ok(session) = request.session.handle() else {
            return;
        };

        // Datagrams and streams are handled at the same time, which is the
        // shape a real application has and the one most likely to expose a
        // handle that cannot be used concurrently.
        let datagrams = {
            let session = session.clone();
            tokio::spawn(async move {
                while let Ok(Some(payload)) = session.receive_datagram().await {
                    let mut answer = b"echo:".to_vec();
                    answer.extend_from_slice(&payload);
                    if session.send_datagram(Bytes::from(answer)).is_err() {
                        break;
                    }
                }
            })
        };

        while let Ok(Some(stream)) = session.accept_stream().await {
            match stream {
                IncomingStream::Bidirectional {
                    mut send,
                    mut receive,
                } => {
                    let body = receive.read_to_end().await.unwrap_or_default();
                    let mut answer = b"echo:".to_vec();
                    answer.extend_from_slice(&body);
                    let _written = send.write(&answer).await;
                    let _finished = send.finish().await;
                }
                IncomingStream::Unidirectional(mut receive) => {
                    let _body = receive.read_to_end().await;
                }
            }
        }

        datagrams.abort();
    }
}

/// Refuses every session.
#[derive(Clone)]
struct RefuseSession;

impl Dispatch for RefuseSession {
    async fn handle_http(&self, _request: HttpRequest) -> HttpResponse {
        HttpResponse::text(StatusCode::OK, "plain")
    }

    async fn handle_webtransport(&self, request: WebTransportRequest) {
        let mut request = request;
        let _refused = request.session.reject(StatusCode::FORBIDDEN).await;
    }
}

/// Opens a stream towards the client instead of waiting for one.
#[derive(Clone)]
struct GreetingSession;

impl Dispatch for GreetingSession {
    async fn handle_http(&self, _request: HttpRequest) -> HttpResponse {
        HttpResponse::text(StatusCode::OK, "plain")
    }

    async fn handle_webtransport(&self, request: WebTransportRequest) {
        let mut request = request;
        if request.session.accept().await.is_err() {
            return;
        }
        let Ok(session) = request.session.handle() else {
            return;
        };

        if let Ok(mut outgoing) = session.open_outgoing().await {
            let _written = outgoing.write(b"greetings").await;
            let _finished = outgoing.finish().await;
        }

        // Stay alive long enough for the client to read what was sent.
        tokio::time::sleep(PATIENCE).await;
    }
}

struct TestServer {
    port: u16,
    controller: Arc<ShutdownController>,
    serving: tokio::task::JoinHandle<()>,
}

impl TestServer {
    fn start<D: Dispatch>(dispatch: D) -> Self {
        let _logging = tracing_subscriber::fmt()
            .with_env_filter(
                tracing_subscriber::EnvFilter::try_from_default_env()
                    .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("off")),
            )
            .with_test_writer()
            .try_init();

        let directory = tempfile::tempdir().expect("a temporary directory");
        let generated =
            rcgen::generate_simple_self_signed(["localhost".to_owned()]).expect("a certificate");
        let certificate = directory.path().join("cert.pem");
        let key = directory.path().join("key.pem");
        std::fs::write(&certificate, generated.cert.pem()).expect("writing the certificate");
        std::fs::write(&key, generated.signing_key.serialize_pem()).expect("writing the key");

        let mut settings = TlsSettings::new(&certificate, &key);
        settings.reload_interval = Duration::ZERO;
        let material = TlsMaterial::load(&settings).expect("the certificate must load");

        let mut config = ServerConfig {
            bind: BindAddress::tcp("localhost", 0),
            http: HttpVersion::Http3,
            tls: Some(settings),
            drain_timeout: Duration::from_secs(5),
            ..Default::default()
        };
        config.validate().expect("the configuration must be valid");

        let sockets = accept::bind(&config).expect("binding must work");
        let port = sockets.local_addresses()[0].port();

        let controller = Arc::new(ShutdownController::new());
        let shutdown = controller.subscribe();
        let drain = DrainCoordinator::new(config.drain_timeout);

        let serving = tokio::spawn(async move {
            // The certificate files must outlive the server that reads them.
            let _directory = directory;
            let _outcome = accept::serve(
                sockets,
                dispatch,
                Arc::new(config),
                Some(material),
                shutdown,
                drain,
            )
            .await;
        });

        Self {
            port,
            controller,
            serving,
        }
    }

    /// Reached by name, as a client would: `localhost` resolves to both
    /// loopback families and the server must answer on whichever one is tried.
    fn url(&self) -> String {
        format!("https://localhost:{}/session", self.port)
    }

    async fn stop(self) {
        self.controller.trigger();
        let _finished = tokio::time::timeout(Duration::from_secs(15), self.serving).await;
    }
}

/// A client that trusts the server's self-signed certificate.
fn client() -> wtransport::Endpoint<wtransport::endpoint::endpoint_side::Client> {
    let config = wtransport::ClientConfig::builder()
        .with_bind_default()
        .with_no_cert_validation()
        .build();
    wtransport::Endpoint::client(config).expect("building a client endpoint")
}

#[tokio::test]
async fn a_session_is_accepted_and_datagrams_round_trip() {
    let accepted = Arc::new(AtomicUsize::new(0));
    let server = TestServer::start(EchoSession {
        accepted: Arc::clone(&accepted),
    });

    let connection = tokio::time::timeout(PATIENCE, client().connect(server.url()))
        .await
        .expect("connecting must not hang")
        .expect("the session must be accepted");

    connection
        .send_datagram(Bytes::from_static(b"ping"))
        .expect("sending a datagram");

    let answer = tokio::time::timeout(PATIENCE, connection.receive_datagram())
        .await
        .expect("a datagram must come back")
        .expect("the session must stay open");

    assert_eq!(&answer.payload()[..], b"echo:ping");
    assert_eq!(accepted.load(Ordering::Acquire), 1);

    server.stop().await;
}

#[tokio::test]
async fn several_datagrams_all_round_trip() {
    let server = TestServer::start(EchoSession {
        accepted: Arc::new(AtomicUsize::new(0)),
    });

    let connection = tokio::time::timeout(PATIENCE, client().connect(server.url()))
        .await
        .expect("connecting must not hang")
        .expect("the session must be accepted");

    for index in 0..5_u8 {
        connection
            .send_datagram(Bytes::copy_from_slice(&[b'a' + index]))
            .expect("sending a datagram");
    }

    // Datagrams may be dropped or reordered, so this asserts that the exchange
    // keeps working rather than that all five arrive in order.
    let mut received = 0;
    for _ in 0..5 {
        match tokio::time::timeout(Duration::from_secs(3), connection.receive_datagram()).await {
            Ok(Ok(datagram)) => {
                assert!(datagram.payload().starts_with(b"echo:"));
                received += 1;
            }
            _ => break,
        }
    }
    assert!(received >= 1, "at least one datagram should come back");

    server.stop().await;
}

#[tokio::test]
async fn a_bidirectional_stream_round_trips() {
    let server = TestServer::start(EchoSession {
        accepted: Arc::new(AtomicUsize::new(0)),
    });

    let connection = tokio::time::timeout(PATIENCE, client().connect(server.url()))
        .await
        .expect("connecting must not hang")
        .expect("the session must be accepted");

    let (mut send, mut receive) = tokio::time::timeout(PATIENCE, async {
        connection
            .open_bi()
            .await
            .expect("opening must be permitted")
            .await
            .expect("the stream must open")
    })
    .await
    .expect("opening must not hang");

    send.write_all(b"hello").await.expect("writing");
    send.finish().await.expect("finishing");

    let mut answer = Vec::new();
    tokio::time::timeout(PATIENCE, receive.read_to_end(&mut answer))
        .await
        .expect("a reply must arrive")
        .expect("reading must succeed");

    assert_eq!(answer, b"echo:hello");
    server.stop().await;
}

#[tokio::test]
async fn a_unidirectional_stream_from_the_client_is_read() {
    let server = TestServer::start(EchoSession {
        accepted: Arc::new(AtomicUsize::new(0)),
    });

    let connection = tokio::time::timeout(PATIENCE, client().connect(server.url()))
        .await
        .expect("connecting must not hang")
        .expect("the session must be accepted");

    let mut send = tokio::time::timeout(PATIENCE, async {
        connection
            .open_uni()
            .await
            .expect("opening must be permitted")
            .await
            .expect("the stream must open")
    })
    .await
    .expect("opening must not hang");

    send.write_all(b"one way").await.expect("writing");
    send.finish().await.expect("finishing");

    // The server reads it and the session stays usable, which a datagram
    // exchange afterwards demonstrates.
    connection
        .send_datagram(Bytes::from_static(b"still here"))
        .expect("sending a datagram");
    let answer = tokio::time::timeout(PATIENCE, connection.receive_datagram())
        .await
        .expect("a datagram must come back")
        .expect("the session must stay open");
    assert_eq!(&answer.payload()[..], b"echo:still here");

    server.stop().await;
}

#[tokio::test]
async fn a_stream_the_server_opens_reaches_the_client() {
    let server = TestServer::start(GreetingSession);

    let connection = tokio::time::timeout(PATIENCE, client().connect(server.url()))
        .await
        .expect("connecting must not hang")
        .expect("the session must be accepted");

    let mut receive = tokio::time::timeout(PATIENCE, connection.accept_uni())
        .await
        .expect("a stream must arrive")
        .expect("the session must stay open");

    let mut greeting = Vec::new();
    tokio::time::timeout(PATIENCE, receive.read_to_end(&mut greeting))
        .await
        .expect("reading must not hang")
        .expect("reading must succeed");

    assert_eq!(greeting, b"greetings");
    server.stop().await;
}

#[tokio::test]
async fn a_refused_session_does_not_connect() {
    let server = TestServer::start(RefuseSession);

    let outcome = tokio::time::timeout(PATIENCE, client().connect(server.url())).await;

    assert!(
        matches!(outcome, Ok(Err(_))),
        "a refused session must fail to connect rather than hang, got {outcome:?}"
    );
    server.stop().await;
}

#[tokio::test]
async fn ordinary_http3_still_works_alongside_sessions() {
    let server = TestServer::start(EchoSession {
        accepted: Arc::new(AtomicUsize::new(0)),
    });

    // The same port serves HTTP over TCP; WebTransport support must not have
    // taken the ordinary path away.
    let client = reqwest::Client::builder()
        .danger_accept_invalid_certs(true)
        .timeout(PATIENCE)
        .build()
        .expect("building an HTTP client");

    let response = client
        .get(format!("https://localhost:{}/", server.port))
        .send()
        .await
        .expect("the request must succeed");
    assert_eq!(response.text().await.unwrap(), "plain");

    server.stop().await;
}
