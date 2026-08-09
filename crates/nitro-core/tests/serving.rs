//! End-to-end tests: real sockets, a real HTTP client, and the drain chain.

use std::net::SocketAddr;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::Duration;

use bytes::Bytes;
use http::StatusCode;
use nitro_core::config::{
    AccessLogConfig, AccessLogFormat, BindAddress, HttpVersion, LogDestination, ServerConfig,
    TlsSettings,
};
use nitro_core::lifecycle::drain::{DrainCoordinator, DrainOutcome};
use nitro_core::lifecycle::signals::ShutdownController;
use nitro_core::streaming;
use nitro_core::transport::tls::TlsMaterial;
use nitro_core::transport::{Dispatch, HttpRequest, HttpResponse, ResponseBody, accept};

/// Answers every request by describing it, so a test can assert on what the
/// transport handed over.
#[derive(Clone, Default)]
struct EchoDispatch {
    seen: Arc<AtomicUsize>,
}

impl Dispatch for EchoDispatch {
    async fn handle_http(&self, request: HttpRequest) -> HttpResponse {
        self.seen.fetch_add(1, Ordering::Release);

        let method = request.parts.method.clone();
        let path = request.parts.path().to_owned();
        let version = request.parts.http_version();
        let scheme = request.parts.scheme.as_str();
        let body = request.body.collect().await.unwrap_or_default();

        HttpResponse::text(
            StatusCode::OK,
            format!(
                "{method} {path} HTTP/{version} {scheme} body={}",
                String::from_utf8_lossy(&body)
            ),
        )
        .with_header("x-echo", &path)
    }
}

/// Holds the connection open and reports when the client goes away.
#[derive(Clone)]
struct StreamingDispatch {
    disconnected: Arc<AtomicBool>,
    chunk_delay: Duration,
}

impl Dispatch for StreamingDispatch {
    async fn handle_http(&self, request: HttpRequest) -> HttpResponse {
        let (sender, body) = streaming::channel(2);
        let disconnected = Arc::clone(&self.disconnected);
        let watcher = request.disconnect.clone();
        let delay = self.chunk_delay;

        tokio::spawn(async move {
            watcher.wait().await;
            disconnected.store(true, Ordering::Release);
        });

        tokio::spawn(async move {
            for index in 0..1000 {
                if sender
                    .send(Bytes::from(format!("chunk-{index}\n")))
                    .await
                    .is_err()
                {
                    break;
                }
                tokio::time::sleep(delay).await;
            }
        });

        HttpResponse::new(StatusCode::OK, ResponseBody::Stream(body))
    }
}

/// Takes long enough to answer that a shutdown can be requested mid-request.
#[derive(Clone)]
struct SlowDispatch {
    delay: Duration,
}

impl Dispatch for SlowDispatch {
    async fn handle_http(&self, _request: HttpRequest) -> HttpResponse {
        tokio::time::sleep(self.delay).await;
        HttpResponse::text(StatusCode::OK, "finished")
    }
}

struct TestServer {
    address: Option<SocketAddr>,
    controller: Arc<ShutdownController>,
    serving: tokio::task::JoinHandle<DrainOutcome>,
}

impl TestServer {
    fn start<D: Dispatch>(mut config: ServerConfig, dispatch: D, tls: Option<TlsMaterial>) -> Self {
        config
            .validate()
            .expect("the test configuration must be valid");
        let sockets = accept::bind(&config).expect("binding an ephemeral port must work");
        let address = sockets.local_addresses().first().copied();

        let controller = Arc::new(ShutdownController::new());
        let shutdown = controller.subscribe();
        let drain = DrainCoordinator::new(config.drain_timeout);

        let serving = tokio::spawn(async move {
            accept::serve(sockets, dispatch, Arc::new(config), tls, shutdown, drain)
                .await
                .expect("serving must not fail")
        });

        Self {
            address,
            controller,
            serving,
        }
    }

    fn base_url(&self, scheme: &str) -> String {
        format!(
            "{scheme}://{}",
            self.address.expect("a TCP server has an address")
        )
    }

    async fn shutdown(self) -> DrainOutcome {
        self.controller.trigger();
        tokio::time::timeout(Duration::from_secs(10), self.serving)
            .await
            .expect("the server must stop")
            .expect("the serving task must not panic")
    }
}

fn cleartext_config() -> ServerConfig {
    ServerConfig {
        bind: BindAddress::tcp("127.0.0.1", 0),
        http: HttpVersion::Http1,
        drain_timeout: Duration::from_secs(5),
        ..Default::default()
    }
}

fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .expect("building a test client")
}

#[tokio::test]
async fn a_request_reaches_the_dispatcher_and_the_response_comes_back() {
    let dispatch = EchoDispatch::default();
    let seen = Arc::clone(&dispatch.seen);
    let server = TestServer::start(cleartext_config(), dispatch, None);

    let response = client()
        .get(format!("{}/hello", server.base_url("http")))
        .send()
        .await
        .expect("the request must succeed");

    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()["x-echo"], "/hello");
    assert_eq!(response.headers()["server"], "nitro");
    let body = response.text().await.unwrap();
    assert_eq!(body, "GET /hello HTTP/1.1 http body=");
    assert_eq!(seen.load(Ordering::Acquire), 1);

    assert!(server.shutdown().await.is_complete());
}

#[tokio::test]
async fn a_request_body_is_readable() {
    let server = TestServer::start(cleartext_config(), EchoDispatch::default(), None);

    let body = client()
        .post(format!("{}/submit", server.base_url("http")))
        .body("payload")
        .send()
        .await
        .expect("the request must succeed")
        .text()
        .await
        .unwrap();

    assert_eq!(body, "POST /submit HTTP/1.1 http body=payload");
    server.shutdown().await;
}

#[tokio::test]
async fn many_requests_share_one_connection() {
    let dispatch = EchoDispatch::default();
    let seen = Arc::clone(&dispatch.seen);
    let server = TestServer::start(cleartext_config(), dispatch, None);
    let client = client();

    for index in 0..5 {
        let response = client
            .get(format!("{}/n/{index}", server.base_url("http")))
            .send()
            .await
            .expect("every request must succeed");
        assert_eq!(response.headers()["x-echo"], format!("/n/{index}"));
    }

    assert_eq!(seen.load(Ordering::Acquire), 5);
    server.shutdown().await;
}

#[tokio::test]
async fn a_streaming_response_is_delivered_in_chunks() {
    let dispatch = StreamingDispatch {
        disconnected: Arc::new(AtomicBool::new(false)),
        chunk_delay: Duration::ZERO,
    };
    let server = TestServer::start(cleartext_config(), dispatch, None);

    let response = client()
        .get(server.base_url("http"))
        .send()
        .await
        .expect("the request must succeed");
    assert!(!response.headers().contains_key("content-length"));

    let body = response.text().await.unwrap();
    assert!(body.starts_with("chunk-0\n"));
    assert!(body.contains("chunk-999\n"));

    server.shutdown().await;
}

#[tokio::test]
async fn a_handler_learns_that_the_client_went_away() {
    let disconnected = Arc::new(AtomicBool::new(false));
    let dispatch = StreamingDispatch {
        disconnected: Arc::clone(&disconnected),
        chunk_delay: Duration::from_millis(20),
    };
    let server = TestServer::start(cleartext_config(), dispatch, None);

    {
        let response = client()
            .get(server.base_url("http"))
            .send()
            .await
            .expect("the request must succeed");
        assert_eq!(response.status(), StatusCode::OK);
        // Dropping the response without reading it closes the connection.
    }

    let mut observed = false;
    for _ in 0..100 {
        if disconnected.load(Ordering::Acquire) {
            observed = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert!(observed, "the handler must be told the client disconnected");

    server.shutdown().await;
}

#[tokio::test]
async fn an_in_flight_request_finishes_during_shutdown() {
    let server = TestServer::start(
        cleartext_config(),
        SlowDispatch {
            delay: Duration::from_millis(300),
        },
        None,
    );
    let url = server.base_url("http");

    let request = tokio::spawn(async move { client().get(url).send().await?.text().await });
    tokio::time::sleep(Duration::from_millis(100)).await;

    let outcome = server.shutdown().await;
    let body = request
        .await
        .expect("the request task must not panic")
        .expect("an accepted request must still be answered");

    assert_eq!(body, "finished");
    assert!(outcome.is_complete());
}

#[tokio::test]
async fn a_stuck_handler_cannot_hold_shutdown_open() {
    let mut config = cleartext_config();
    config.drain_timeout = Duration::from_millis(200);

    let server = TestServer::start(
        config,
        SlowDispatch {
            delay: Duration::from_secs(60),
        },
        None,
    );
    let url = server.base_url("http");

    let request = tokio::spawn(async move { client().get(url).send().await });
    tokio::time::sleep(Duration::from_millis(100)).await;

    let outcome = tokio::time::timeout(Duration::from_secs(5), server.shutdown())
        .await
        .expect("shutdown must not wait for the stuck handler");
    assert!(!outcome.connections_finished);
    request.abort();
}

#[tokio::test]
async fn no_new_connection_is_served_after_shutdown() {
    let server = TestServer::start(cleartext_config(), EchoDispatch::default(), None);
    let url = server.base_url("http");

    server.shutdown().await;

    let result = client().get(&url).send().await;
    assert!(result.is_err(), "the port must not answer after shutdown");
}

#[tokio::test]
async fn tls_and_http2_are_negotiated_over_alpn() {
    let directory = tempfile::tempdir().unwrap();
    let generated = rcgen::generate_simple_self_signed(["localhost".to_owned()]).unwrap();
    let certificate_path = directory.path().join("cert.pem");
    let key_path = directory.path().join("key.pem");
    std::fs::write(&certificate_path, generated.cert.pem()).unwrap();
    std::fs::write(&key_path, generated.signing_key.serialize_pem()).unwrap();

    let mut settings = TlsSettings::new(&certificate_path, &key_path);
    settings.reload_interval = Duration::ZERO;
    let material = TlsMaterial::load(&settings).expect("the test certificate must load");

    let config = ServerConfig {
        bind: BindAddress::tcp("127.0.0.1", 0),
        http: HttpVersion::Http2,
        tls: Some(settings),
        drain_timeout: Duration::from_secs(5),
        ..Default::default()
    };
    let server = TestServer::start(config, EchoDispatch::default(), Some(material));

    let port = server.address.unwrap().port();
    let client = reqwest::Client::builder()
        .add_root_certificate(
            reqwest::Certificate::from_pem(generated.cert.pem().as_bytes()).unwrap(),
        )
        .resolve("localhost", format!("127.0.0.1:{port}").parse().unwrap())
        .timeout(Duration::from_secs(10))
        .build()
        .expect("building a TLS test client");

    let response = client
        .get(format!("https://localhost:{port}/secure"))
        .send()
        .await
        .expect("the TLS request must succeed");

    assert_eq!(response.version(), reqwest::Version::HTTP_2);
    let body = response.text().await.unwrap();
    assert_eq!(body, "GET /secure HTTP/2 https body=");

    server.shutdown().await;
}

#[cfg(unix)]
#[tokio::test]
async fn a_unix_socket_serves_requests() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("nitro.sock");

    let config = ServerConfig {
        bind: BindAddress::Unix { path: path.clone() },
        http: HttpVersion::Http1,
        drain_timeout: Duration::from_secs(5),
        ..Default::default()
    };
    let server = TestServer::start(config, EchoDispatch::default(), None);

    let mut stream = tokio::net::UnixStream::connect(&path)
        .await
        .expect("connecting to the socket file must work");
    stream
        .write_all(b"GET /over-uds HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .await
        .unwrap();

    let mut response = String::new();
    stream.read_to_string(&mut response).await.unwrap();

    assert!(response.starts_with("HTTP/1.1 200 OK\r\n"));
    assert!(response.contains("x-echo: /over-uds"));
    assert!(response.ends_with("GET /over-uds HTTP/1.1 http body="));

    server.shutdown().await;
}

#[tokio::test]
async fn access_log_entries_are_written() {
    let directory = tempfile::tempdir().unwrap();
    let log_path = directory.path().join("access.log");

    let mut config = cleartext_config();
    config.access_log = Some(AccessLogConfig {
        destination: LogDestination::File(log_path.clone()),
        format: AccessLogFormat::Combined,
    });

    let server = TestServer::start(config, EchoDispatch::default(), None);
    client()
        .get(format!("{}/logged?x=1", server.base_url("http")))
        .header("user-agent", "probe/1.0")
        .send()
        .await
        .expect("the request must succeed");
    server.shutdown().await;

    let written = std::fs::read_to_string(&log_path).expect("the log file must exist");
    assert!(written.contains("\"GET /logged?x=1 HTTP/1.1\" 200"));
    assert!(written.contains("\"probe/1.0\""));
    assert!(written.starts_with("127.0.0.1 - - ["));
}

#[tokio::test]
async fn concurrent_connections_are_capped() {
    let mut config = cleartext_config();
    config.max_concurrent_connections = Some(1);

    let dispatch = StreamingDispatch {
        disconnected: Arc::new(AtomicBool::new(false)),
        chunk_delay: Duration::from_millis(50),
    };
    let server = TestServer::start(config, dispatch, None);
    let address = server.address.unwrap();

    // The first connection takes the only slot and keeps it while it streams.
    let mut held = tokio::net::TcpStream::connect(address).await.unwrap();
    {
        use tokio::io::AsyncWriteExt;
        held.write_all(b"GET /first HTTP/1.1\r\nHost: localhost\r\n\r\n")
            .await
            .unwrap();
    }
    tokio::time::sleep(Duration::from_millis(200)).await;

    // A second connection is accepted by the kernel but not served, so no
    // response arrives while the slot is taken.
    let mut queued = tokio::net::TcpStream::connect(address).await.unwrap();
    {
        use tokio::io::AsyncWriteExt;
        queued
            .write_all(b"GET /second HTTP/1.1\r\nHost: localhost\r\n\r\n")
            .await
            .unwrap();
    }

    let mut buffer = [0_u8; 16];
    let read = tokio::time::timeout(
        Duration::from_millis(300),
        tokio::io::AsyncReadExt::read(&mut queued, &mut buffer),
    )
    .await;
    assert!(
        read.is_err(),
        "a connection beyond the limit must wait rather than be served"
    );

    drop(held);
    drop(queued);
    server.shutdown().await;
}

// ── WebSocket ────────────────────────────────────────────────────────────────

use nitro_core::transport::WebSocketRequest;
use nitro_core::websocket::{WebSocketError, WebSocketMessage};

/// Accepts every upgrade and echoes what it receives.
#[derive(Clone)]
struct EchoSocket {
    subprotocol: Option<String>,
    accepted: Arc<AtomicUsize>,
}

impl Dispatch for EchoSocket {
    async fn handle_http(&self, _request: HttpRequest) -> HttpResponse {
        HttpResponse::text(StatusCode::OK, "plain")
    }

    async fn handle_websocket(&self, request: WebSocketRequest) {
        let mut request = request;
        let offered = request.handshake.subprotocols().to_vec();

        let mut connection = match request.handshake.accept(self.subprotocol.clone()).await {
            Ok(connection) => connection,
            Err(error) => {
                eprintln!("accepting failed: {error}");
                return;
            }
        };
        self.accepted.fetch_add(1, Ordering::Release);

        if !offered.is_empty() {
            let _sent = connection
                .send(WebSocketMessage::Text(format!(
                    "offered:{}",
                    offered.join("+")
                )))
                .await;
        }

        loop {
            match connection.receive().await {
                Some(Ok(WebSocketMessage::Text(text))) => {
                    if connection
                        .send(WebSocketMessage::Text(format!("echo:{text}")))
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
                Some(Ok(WebSocketMessage::Binary(data))) => {
                    if connection
                        .send(WebSocketMessage::Binary(data))
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
                Some(Ok(WebSocketMessage::Close { .. })) | None => break,
                Some(Ok(_)) => {}
                Some(Err(WebSocketError::Closed)) => break,
                Some(Err(error)) => {
                    eprintln!("receive failed: {error}");
                    break;
                }
            }
        }
    }
}

/// Refuses every upgrade.
#[derive(Clone)]
struct RefuseSocket;

impl Dispatch for RefuseSocket {
    async fn handle_http(&self, _request: HttpRequest) -> HttpResponse {
        HttpResponse::text(StatusCode::OK, "plain")
    }

    async fn handle_websocket(&self, request: WebSocketRequest) {
        let mut request = request;
        let _refused = request
            .handshake
            .reject(StatusCode::FORBIDDEN, "not for you");
    }
}

/// Returns without answering the handshake at all.
#[derive(Clone)]
struct SilentSocket;

impl Dispatch for SilentSocket {
    async fn handle_http(&self, _request: HttpRequest) -> HttpResponse {
        HttpResponse::text(StatusCode::OK, "plain")
    }

    async fn handle_websocket(&self, _request: WebSocketRequest) {}
}

async fn connect(
    server: &TestServer,
    subprotocols: &[&str],
) -> Result<
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>,
    tokio_tungstenite::tungstenite::Error,
> {
    use tokio_tungstenite::tungstenite::client::IntoClientRequest;

    let mut request = format!("ws://{}/socket", server.address.unwrap())
        .into_client_request()
        .expect("a well-formed request");
    if !subprotocols.is_empty() {
        request.headers_mut().insert(
            "sec-websocket-protocol",
            subprotocols.join(", ").parse().unwrap(),
        );
    }
    tokio_tungstenite::connect_async(request)
        .await
        .map(|(stream, _response)| stream)
}

#[tokio::test]
async fn a_websocket_upgrade_is_accepted_and_messages_round_trip() {
    use futures_util::{SinkExt, StreamExt};
    use tokio_tungstenite::tungstenite::Message;

    let accepted = Arc::new(AtomicUsize::new(0));
    let dispatch = EchoSocket {
        subprotocol: None,
        accepted: Arc::clone(&accepted),
    };
    let server = TestServer::start(cleartext_config(), dispatch, None);

    let mut socket = connect(&server, &[])
        .await
        .expect("the upgrade must succeed");
    socket.send(Message::Text("hello".into())).await.unwrap();

    let reply = socket.next().await.expect("a reply").unwrap();
    assert_eq!(reply.into_text().unwrap().as_str(), "echo:hello");

    socket
        .send(Message::Binary(Bytes::from_static(b"\x01\x02")))
        .await
        .unwrap();
    let reply = socket.next().await.expect("a reply").unwrap();
    assert_eq!(reply.into_data(), Bytes::from_static(b"\x01\x02"));

    socket.close(None).await.unwrap();
    assert_eq!(accepted.load(Ordering::Acquire), 1);
    server.shutdown().await;
}

#[tokio::test]
async fn a_subprotocol_the_client_offered_is_selected() {
    use futures_util::StreamExt;

    let dispatch = EchoSocket {
        subprotocol: Some("chat".to_owned()),
        accepted: Arc::new(AtomicUsize::new(0)),
    };
    let server = TestServer::start(cleartext_config(), dispatch, None);

    let mut socket = connect(&server, &["chat", "superchat"])
        .await
        .expect("the upgrade must succeed");

    let greeting = socket.next().await.expect("a greeting").unwrap();
    assert_eq!(
        greeting.into_text().unwrap().as_str(),
        "offered:chat+superchat"
    );
    server.shutdown().await;
}

#[tokio::test]
async fn a_refused_upgrade_answers_with_an_ordinary_response() {
    let server = TestServer::start(cleartext_config(), RefuseSocket, None);

    let error = connect(&server, &[])
        .await
        .expect_err("the upgrade must be refused");
    let message = error.to_string();
    assert!(
        message.contains("403") || message.to_lowercase().contains("forbidden"),
        "the refusal should surface as an HTTP status, got {message}"
    );

    server.shutdown().await;
}

#[tokio::test]
async fn a_handler_that_never_answers_does_not_leave_the_client_waiting() {
    let server = TestServer::start(cleartext_config(), SilentSocket, None);

    let outcome = tokio::time::timeout(Duration::from_secs(10), connect(&server, &[])).await;
    assert!(
        matches!(outcome, Ok(Err(_))),
        "an unanswered handshake must still produce a response"
    );

    server.shutdown().await;
}

#[tokio::test]
async fn an_ordinary_request_is_unaffected_by_websocket_support() {
    let dispatch = EchoSocket {
        subprotocol: None,
        accepted: Arc::new(AtomicUsize::new(0)),
    };
    let server = TestServer::start(cleartext_config(), dispatch, None);

    let response = client()
        .get(server.base_url("http"))
        .send()
        .await
        .expect("the request must succeed");
    assert_eq!(response.text().await.unwrap(), "plain");

    server.shutdown().await;
}

#[tokio::test]
async fn upgrades_can_be_switched_off() {
    let mut config = cleartext_config();
    config.websockets = false;

    let dispatch = EchoSocket {
        subprotocol: None,
        accepted: Arc::new(AtomicUsize::new(0)),
    };
    let server = TestServer::start(config, dispatch, None);

    assert!(
        connect(&server, &[]).await.is_err(),
        "with WebSocket off, an upgrade must not be honoured"
    );
    server.shutdown().await;
}

// ── HTTP/3 ───────────────────────────────────────────────────────────────────

/// A server certificate and the client configuration that trusts it.
fn quic_material(directory: &tempfile::TempDir) -> (TlsSettings, Vec<u8>) {
    let generated = rcgen::generate_simple_self_signed(["localhost".to_owned()]).unwrap();
    let certificate_path = directory.path().join("cert.pem");
    let key_path = directory.path().join("key.pem");
    std::fs::write(&certificate_path, generated.cert.pem()).unwrap();
    std::fs::write(&key_path, generated.signing_key.serialize_pem()).unwrap();

    let mut settings = TlsSettings::new(&certificate_path, &key_path);
    settings.reload_interval = Duration::ZERO;
    (settings, generated.cert.der().to_vec())
}

fn quic_client(certificate: Vec<u8>) -> quinn::Endpoint {
    let mut roots = rustls::RootCertStore::empty();
    roots
        .add(rustls::pki_types::CertificateDer::from(certificate))
        .unwrap();

    let mut tls = rustls::ClientConfig::builder_with_provider(std::sync::Arc::new(
        rustls::crypto::ring::default_provider(),
    ))
    .with_protocol_versions(&[&rustls::version::TLS13])
    .unwrap()
    .with_root_certificates(roots)
    .with_no_client_auth();
    tls.alpn_protocols = vec![b"h3".to_vec()];

    let crypto = quinn::crypto::rustls::QuicClientConfig::try_from(tls).unwrap();
    let mut endpoint = quinn::Endpoint::client("127.0.0.1:0".parse().unwrap()).unwrap();
    endpoint.set_default_client_config(quinn::ClientConfig::new(std::sync::Arc::new(crypto)));
    endpoint
}

#[tokio::test]
async fn an_http3_request_is_served() {
    let directory = tempfile::tempdir().unwrap();
    let (settings, certificate) = quic_material(&directory);
    let material = TlsMaterial::load(&settings).unwrap();

    let config = ServerConfig {
        bind: BindAddress::tcp("127.0.0.1", 0),
        http: HttpVersion::Http3,
        tls: Some(settings),
        drain_timeout: Duration::from_secs(5),
        ..Default::default()
    };
    let server = TestServer::start(config, EchoDispatch::default(), Some(material));
    let address = server.address.unwrap();

    let endpoint = quic_client(certificate);
    let connection = endpoint
        .connect(address, "localhost")
        .expect("connecting must be configurable")
        .await
        .expect("the QUIC handshake must succeed");

    let (mut driver, mut sender) = h3::client::new(h3_quinn::Connection::new(connection))
        .await
        .expect("the HTTP/3 layer must come up");

    let driving =
        tokio::spawn(
            async move { std::future::poll_fn(|context| driver.poll_close(context)).await },
        );

    let request = http::Request::builder()
        .method("GET")
        .uri(format!("https://localhost:{}/over-quic", address.port()))
        .body(())
        .unwrap();

    let mut stream = sender
        .send_request(request)
        .await
        .expect("sending must work");
    stream.finish().await.expect("finishing the request body");

    let response = stream
        .recv_response()
        .await
        .expect("a response must arrive");
    assert_eq!(response.status(), StatusCode::OK);

    let mut body = Vec::new();
    while let Some(mut chunk) = stream.recv_data().await.expect("reading the body") {
        use bytes::Buf;
        let length = chunk.remaining();
        body.extend_from_slice(&chunk.copy_to_bytes(length));
    }
    assert_eq!(
        String::from_utf8(body).unwrap(),
        "GET /over-quic HTTP/3 https body="
    );

    drop(sender);
    let _closed = tokio::time::timeout(Duration::from_secs(2), driving).await;
    endpoint.close(0u32.into(), b"done");
    let _ = tokio::time::timeout(Duration::from_secs(10), server.shutdown()).await;
}

#[tokio::test]
async fn http3_and_tcp_share_a_port_and_an_application() {
    let directory = tempfile::tempdir().unwrap();
    let (settings, certificate) = quic_material(&directory);
    let material = TlsMaterial::load(&settings).unwrap();

    let config = ServerConfig {
        bind: BindAddress::tcp("127.0.0.1", 0),
        http: HttpVersion::Http3,
        tls: Some(settings),
        drain_timeout: Duration::from_secs(5),
        ..Default::default()
    };
    let dispatch = EchoDispatch::default();
    let seen = Arc::clone(&dispatch.seen);
    let server = TestServer::start(config, dispatch, Some(material));
    let port = server.address.unwrap().port();

    // The same port answers over TCP with TLS, and advertises HTTP/3 while
    // doing so.
    let tcp = reqwest::Client::builder()
        .add_root_certificate(reqwest::Certificate::from_der(&certificate).unwrap())
        .resolve("localhost", format!("127.0.0.1:{port}").parse().unwrap())
        .timeout(Duration::from_secs(10))
        .build()
        .unwrap();

    let response = tcp
        .get(format!("https://localhost:{port}/over-tcp"))
        .send()
        .await
        .expect("the TCP request must succeed");
    assert_eq!(response.headers()["alt-svc"], format!("h3=\":{port}\""));
    assert_eq!(
        response.text().await.unwrap(),
        "GET /over-tcp HTTP/2 https body="
    );
    assert_eq!(seen.load(Ordering::Acquire), 1);

    let _ = tokio::time::timeout(Duration::from_secs(10), server.shutdown()).await;
}
