//! The metrics endpoint, scraped over a real socket while real requests are
//! served.
//!
//! Every test here shares one process-global metric registry, so assertions
//! look for the series a test produced rather than for exact totals.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use http::StatusCode;
use nitro_core::config::{BindAddress, HttpVersion, ServerConfig};
use nitro_core::lifecycle::drain::DrainCoordinator;
use nitro_core::lifecycle::signals::ShutdownController;
use nitro_core::observability::ExporterConfig;
use nitro_core::transport::{Dispatch, HttpRequest, HttpResponse, accept};

/// Answers with the status the path asks for, and reports the route it was
/// reached by so the label can be asserted on.
#[derive(Clone)]
struct RoutedDispatch {
    route: &'static str,
    status: StatusCode,
}

impl Dispatch for RoutedDispatch {
    async fn handle_http(&self, _request: HttpRequest) -> HttpResponse {
        HttpResponse::text(self.status, "answered").from_route(Some(self.route.to_owned()))
    }
}

struct TestServer {
    address: SocketAddr,
    metrics: SocketAddr,
    controller: Arc<ShutdownController>,
    serving: tokio::task::JoinHandle<()>,
}

impl TestServer {
    fn start<D: Dispatch>(dispatch: D) -> Self {
        let mut config = ServerConfig {
            bind: BindAddress::tcp("localhost", 0),
            http: HttpVersion::Http1,
            drain_timeout: Duration::from_secs(5),
            observability: ExporterConfig {
                enabled: true,
                host: "localhost".to_owned(),
                port: 0,
            },
            ..Default::default()
        };
        config
            .validate()
            .expect("the test configuration must be valid");

        let sockets = accept::bind(&config).expect("binding ephemeral ports must work");
        let address = *sockets
            .local_addresses()
            .first()
            .expect("the application has an address");
        let metrics = *sockets
            .metrics
            .first()
            .expect("one exporter per worker, and this is a single worker")
            .addresses()
            .first()
            .expect("the exporter has an address");

        let controller = Arc::new(ShutdownController::new());
        let shutdown = controller.subscribe();
        let drain = DrainCoordinator::new(config.drain_timeout);

        let serving = tokio::spawn(async move {
            accept::serve(sockets, dispatch, Arc::new(config), None, shutdown, drain)
                .await
                .expect("serving must not fail");
        });

        Self {
            address,
            metrics,
            controller,
            serving,
        }
    }

    async fn scrape(&self) -> (StatusCode, String) {
        let response = client()
            .get(format!("http://{}/metrics", self.metrics))
            .send()
            .await
            .expect("the exporter must answer");
        let status = response.status();
        let body = response.text().await.expect("the body must be readable");
        (
            StatusCode::from_u16(status.as_u16()).expect("a valid status"),
            body,
        )
    }

    async fn shutdown(self) {
        self.controller.trigger();
        tokio::time::timeout(Duration::from_secs(10), self.serving)
            .await
            .expect("the server must stop")
            .expect("the serving task must not panic");
    }
}

fn install_crypto_provider() {
    static ONCE: std::sync::Once = std::sync::Once::new();
    ONCE.call_once(|| {
        let _installed = rustls::crypto::ring::default_provider().install_default();
    });
}

fn client() -> reqwest::Client {
    install_crypto_provider();
    reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .expect("building a test client")
}

#[tokio::test]
async fn a_served_request_shows_up_in_a_scrape() {
    let server = TestServer::start(RoutedDispatch {
        route: "/scraped/<int:id>",
        status: StatusCode::OK,
    });

    let answered = client()
        .get(format!("http://{}/scraped/7", server.address))
        .send()
        .await
        .expect("the application must answer");
    assert_eq!(answered.status(), 200);

    let (status, body) = server.scrape().await;

    assert_eq!(status, StatusCode::OK);
    assert!(
        body.contains(r#"route="/scraped/<int:id>""#),
        "the pattern is the label, not the requested path: {body}"
    );
    assert!(
        !body.contains("/scraped/7"),
        "the concrete path must not become its own series: {body}"
    );
    assert!(body.contains(r#"method="GET""#));
    assert!(body.contains(r#"status="2xx""#));
    assert!(body.contains("nitro_http_request_duration_seconds_bucket"));

    server.shutdown().await;
}

#[tokio::test]
async fn the_exporter_listens_on_its_own_port() {
    let server = TestServer::start(RoutedDispatch {
        route: "/separate",
        status: StatusCode::OK,
    });

    assert_ne!(
        server.metrics.port(),
        server.address.port(),
        "metrics must not share the application's port"
    );

    // The application's port does not serve metrics, and the metrics port does
    // not serve the application.
    let on_app_port = client()
        .get(format!("http://{}/metrics", server.address))
        .send()
        .await
        .expect("the application must answer");
    assert!(
        !on_app_port
            .text()
            .await
            .unwrap_or_default()
            .contains("# TYPE nitro_http_requests_total")
    );

    let elsewhere = client()
        .get(format!("http://{}/anything-else", server.metrics))
        .send()
        .await
        .expect("the exporter must answer");
    assert_eq!(elsewhere.status(), 404);

    server.shutdown().await;
}

#[tokio::test]
async fn the_exporter_binds_to_loopback_only() {
    let server = TestServer::start(RoutedDispatch {
        route: "/loopback",
        status: StatusCode::OK,
    });

    assert!(
        server.metrics.ip().is_loopback(),
        "the default host must not expose metrics to the network, got {}",
        server.metrics.ip()
    );

    server.shutdown().await;
}

#[tokio::test]
async fn a_scrape_is_prometheus_text_exposition() {
    let server = TestServer::start(RoutedDispatch {
        route: "/exposition",
        status: StatusCode::OK,
    });

    let response = client()
        .get(format!("http://{}/metrics", server.metrics))
        .send()
        .await
        .expect("the exporter must answer");

    let content_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .to_owned();
    let body = response.text().await.expect("the body must be readable");

    assert!(
        content_type.starts_with("text/plain") && content_type.contains("version=0.0.4"),
        "unexpected content type: {content_type}"
    );
    assert!(body.contains("# HELP nitro_connections_total"));
    assert!(body.contains("# TYPE nitro_connections_active gauge"));
    assert!(body.contains("nitro_worker_start_time_seconds"));

    server.shutdown().await;
}

#[tokio::test]
async fn an_error_response_is_counted_by_class() {
    let server = TestServer::start(RoutedDispatch {
        route: "/failing",
        status: StatusCode::INTERNAL_SERVER_ERROR,
    });

    let answered = client()
        .get(format!("http://{}/failing", server.address))
        .send()
        .await
        .expect("the application must answer");
    assert_eq!(answered.status(), 500);

    let (_, body) = server.scrape().await;
    let counted = body
        .lines()
        .find(|line| line.starts_with("nitro_http_requests_total") && line.contains("/failing"))
        .unwrap_or_else(|| panic!("the failing route was not counted: {body}"));

    assert!(counted.contains(r#"status="5xx""#), "unexpected: {counted}");

    server.shutdown().await;
}

#[tokio::test]
async fn each_worker_gets_an_endpoint_of_its_own() {
    let config = ServerConfig {
        bind: BindAddress::tcp("localhost", 0),
        http: HttpVersion::Http1,
        workers: 3,
        observability: ExporterConfig {
            enabled: true,
            host: "localhost".to_owned(),
            port: 0,
        },
        ..Default::default()
    };

    let sockets = accept::bind(&config).expect("binding must work");
    let ports: std::collections::BTreeSet<u16> = sockets
        .metrics
        .iter()
        .flat_map(|endpoint| endpoint.addresses())
        .map(|address| address.port())
        .collect();

    assert_eq!(sockets.metrics.len(), 3);
    assert_eq!(
        ports.len(),
        3,
        "separate processes keep separate counters, so they cannot share a port"
    );
}

#[tokio::test]
async fn nothing_listens_when_observability_is_off() {
    let config = ServerConfig {
        bind: BindAddress::tcp("localhost", 0),
        http: HttpVersion::Http1,
        ..Default::default()
    };

    let sockets = accept::bind(&config).expect("binding must work");

    assert!(
        sockets.metrics.is_empty(),
        "the exporter is opt-in and must not bind by default"
    );
}

#[tokio::test]
async fn the_exporter_stops_with_the_server() {
    let server = TestServer::start(RoutedDispatch {
        route: "/stopping",
        status: StatusCode::OK,
    });
    let metrics = server.metrics;

    server.shutdown().await;

    let after = client()
        .get(format!("http://{metrics}/metrics"))
        .send()
        .await;

    assert!(
        after.is_err(),
        "the exporter must not outlive the server it reports on"
    );
}
