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
