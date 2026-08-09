//! Server configuration structures.
//!
//! These are plain Rust values with no knowledge of where they came from. The
//! binding crate is responsible for translating a project's settings object
//! into a [`ServerConfig`] and calling [`ServerConfig::validate`] before any
//! socket is bound.

use std::net::SocketAddr;

use nitro_observability::ExporterConfig;
use std::path::PathBuf;
use std::time::Duration;

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum ConfigError {
    #[error("{field} must be at least {minimum}, got {value}")]
    TooSmall {
        field: &'static str,
        minimum: usize,
        value: usize,
    },
    #[error("HTTP/3 requires a TLS certificate and key")]
    Http3WithoutTls,
    #[error("client certificate verification requires a certificate authority bundle")]
    ClientAuthWithoutAuthority,
    #[error("{0}")]
    Invalid(String),
}

/// Where the server listens. A hostname is resolved at bind time, and every
/// address it resolves to gets its own listening socket.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BindAddress {
    Tcp { host: String, port: u16 },
    Unix { path: PathBuf },
}

impl BindAddress {
    pub fn tcp(host: impl Into<String>, port: u16) -> Self {
        Self::Tcp {
            host: host.into(),
            port,
        }
    }

    pub fn port(&self) -> Option<u16> {
        match self {
            Self::Tcp { port, .. } => Some(*port),
            Self::Unix { .. } => None,
        }
    }
}

/// The highest HTTP version the server will negotiate. Every version below the
/// selected one stays available; HTTP/1.1 is always the TCP baseline.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum HttpVersion {
    Http1,
    Http2,
    #[default]
    Http3,
}

impl HttpVersion {
    pub fn h2_enabled(self) -> bool {
        matches!(self, Self::Http2 | Self::Http3)
    }

    pub fn h3_enabled(self) -> bool {
        matches!(self, Self::Http3)
    }

    /// ALPN identifiers to offer during a TLS handshake on the TCP socket, in
    /// descending order of preference.
    pub fn tcp_alpn(self) -> Vec<Vec<u8>> {
        if self.h2_enabled() {
            vec![b"h2".to_vec(), b"http/1.1".to_vec()]
        } else {
            vec![b"http/1.1".to_vec()]
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub enum ClientAuth {
    #[default]
    None,
    /// Verify a presented client certificate, but allow connections without one.
    Optional(PathBuf),
    /// Reject connections that do not present a valid client certificate.
    Required(PathBuf),
}

impl ClientAuth {
    pub fn authority(&self) -> Option<&PathBuf> {
        match self {
            Self::None => None,
            Self::Optional(path) | Self::Required(path) => Some(path),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TlsSettings {
    pub certificate: PathBuf,
    pub private_key: PathBuf,
    pub client_auth: ClientAuth,
    /// Terminate TLS on the TCP socket. Turn this off when a reverse proxy
    /// already does so; QUIC keeps its own TLS either way because the protocol
    /// requires it.
    pub terminate_tcp: bool,
    /// How often the certificate file is checked for replacement. Zero disables
    /// reloading entirely.
    pub reload_interval: Duration,
}

impl TlsSettings {
    pub fn new(certificate: impl Into<PathBuf>, private_key: impl Into<PathBuf>) -> Self {
        Self {
            certificate: certificate.into(),
            private_key: private_key.into(),
            client_auth: ClientAuth::None,
            terminate_tcp: true,
            reload_interval: Duration::from_secs(10),
        }
    }
}

/// Controls the `Alt-Svc` response header that advertises HTTP/3 availability.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub enum AltSvc {
    /// Derive the header from the bind port whenever HTTP/3 is active.
    #[default]
    Auto,
    /// Send nothing, for deployments where a proxy advertises its own endpoint.
    Off,
    /// Send this value verbatim, for a public port that differs from the bind port.
    Custom(String),
}

impl AltSvc {
    /// The header value to send, if any.
    ///
    /// `port` is the port actually being served, which is not always the one
    /// configured: a request to bind port zero is answered by the kernel with a
    /// real port, and advertising the zero would send clients nowhere.
    pub fn header_value(&self, http: HttpVersion, port: Option<u16>) -> Option<String> {
        match self {
            Self::Custom(value) => Some(value.clone()),
            Self::Off => None,
            Self::Auto if http.h3_enabled() => port.map(|port| format!("h3=\":{port}\"")),
            Self::Auto => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum LogLevel {
    Trace,
    Debug,
    #[default]
    Info,
    Warning,
    Error,
}

impl LogLevel {
    pub fn as_filter(self) -> &'static str {
        match self {
            Self::Trace => "trace",
            Self::Debug => "debug",
            Self::Info => "info",
            Self::Warning => "warn",
            Self::Error => "error",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub enum LogDestination {
    Stdout,
    #[default]
    Stderr,
    File(PathBuf),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum LogFormat {
    #[default]
    Pretty,
    Json,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct LoggingConfig {
    pub level: LogLevel,
    pub destination: LogDestination,
    pub format: LogFormat,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum AccessLogFormat {
    #[default]
    Combined,
    Json,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccessLogConfig {
    pub destination: LogDestination,
    pub format: AccessLogFormat,
}

impl Default for AccessLogConfig {
    fn default() -> Self {
        Self {
            destination: LogDestination::Stdout,
            format: AccessLogFormat::default(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServerConfig {
    pub bind: BindAddress,
    pub tls: Option<TlsSettings>,
    pub http: HttpVersion,
    pub websockets: bool,
    pub webtransport: bool,
    pub workers: usize,
    pub runtime_threads: usize,
    pub backlog: u32,
    /// Upper bound on simultaneously served connections per worker. Connections
    /// beyond it wait for a slot rather than being dropped.
    pub max_concurrent_connections: Option<usize>,
    /// Per-session datagram ring depth. The oldest datagram is dropped when full.
    pub datagram_queue_capacity: usize,
    /// Depth of a streaming response's chunk channel. This is what turns
    /// `send_bytes` into a real awaitable: once this many chunks are queued the
    /// producer waits for the transport to catch up.
    pub stream_queue_capacity: usize,
    pub alt_svc: AltSvc,
    /// How long in-flight connections are given to finish once draining starts.
    pub drain_timeout: Duration,
    /// Value for the `Server` response header. `None` omits the header.
    pub server_header: Option<String>,
    pub logging: LoggingConfig,
    pub access_log: Option<AccessLogConfig>,
    /// Where metrics are exposed. Off by default, and on its own port when on.
    pub observability: ExporterConfig,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            bind: BindAddress::tcp("localhost", 8000),
            tls: None,
            http: HttpVersion::default(),
            websockets: true,
            webtransport: true,
            workers: 1,
            runtime_threads: 1,
            backlog: 1024,
            max_concurrent_connections: None,
            datagram_queue_capacity: 64,
            stream_queue_capacity: 16,
            alt_svc: AltSvc::default(),
            drain_timeout: Duration::from_secs(30),
            server_header: Some("nitro".to_owned()),
            logging: LoggingConfig::default(),
            access_log: None,
            observability: ExporterConfig::default(),
        }
    }
}

impl ServerConfig {
    /// Apply the adjustments that depend on other fields, then reject anything
    /// that cannot produce a working server.
    ///
    /// WebTransport rides on HTTP/3, so it is switched off rather than reported
    /// as an error when HTTP/3 is unavailable — a caller that lowered the HTTP
    /// version has already expressed the stronger preference.
    pub fn validate(&mut self) -> Result<(), ConfigError> {
        if !self.http.h3_enabled() {
            self.webtransport = false;
        }

        if self.http.h3_enabled() && self.tls.is_none() {
            return Err(ConfigError::Http3WithoutTls);
        }

        if let Some(tls) = &self.tls
            && !matches!(tls.client_auth, ClientAuth::None)
            && tls.client_auth.authority().is_none()
        {
            return Err(ConfigError::ClientAuthWithoutAuthority);
        }

        if matches!(self.bind, BindAddress::Unix { .. }) && self.http.h3_enabled() {
            return Err(ConfigError::Invalid(
                "HTTP/3 needs a UDP socket and cannot run on a Unix domain socket".to_owned(),
            ));
        }

        for (field, value) in [
            ("workers", self.workers),
            ("runtime_threads", self.runtime_threads),
            ("datagram_queue_capacity", self.datagram_queue_capacity),
            ("stream_queue_capacity", self.stream_queue_capacity),
        ] {
            if value < 1 {
                return Err(ConfigError::TooSmall {
                    field,
                    minimum: 1,
                    value,
                });
            }
        }

        if let Some(limit) = self.max_concurrent_connections
            && limit < 1
        {
            return Err(ConfigError::TooSmall {
                field: "max_concurrent_connections",
                minimum: 1,
                value: limit,
            });
        }

        Ok(())
    }

    /// TLS terminates on the TCP socket only when configured and not delegated.
    pub fn tcp_tls(&self) -> Option<&TlsSettings> {
        self.tls.as_ref().filter(|tls| tls.terminate_tcp)
    }
}

/// Addresses of the two ends of a connection, recorded once at accept time.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ConnectionAddresses {
    pub client: Option<SocketAddr>,
    pub server: Option<SocketAddr>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn http3_downgrade_disables_webtransport() {
        let mut config = ServerConfig {
            http: HttpVersion::Http2,
            webtransport: true,
            ..Default::default()
        };
        config.validate().expect("http/2 without tls is valid");
        assert!(!config.webtransport);
    }

    #[test]
    fn http3_without_tls_is_rejected() {
        let mut config = ServerConfig::default();
        assert_eq!(config.validate(), Err(ConfigError::Http3WithoutTls));
    }

    #[test]
    fn http3_with_tls_keeps_webtransport() {
        let mut config = ServerConfig {
            tls: Some(TlsSettings::new("cert.pem", "key.pem")),
            ..Default::default()
        };
        config.validate().expect("http/3 with tls is valid");
        assert!(config.webtransport);
    }

    #[test]
    fn zero_workers_is_rejected() {
        let mut config = ServerConfig {
            http: HttpVersion::Http1,
            workers: 0,
            ..Default::default()
        };
        assert_eq!(
            config.validate(),
            Err(ConfigError::TooSmall {
                field: "workers",
                minimum: 1,
                value: 0
            })
        );
    }

    #[test]
    fn unix_socket_cannot_serve_http3() {
        let mut config = ServerConfig {
            bind: BindAddress::Unix {
                path: "/tmp/nitro.sock".into(),
            },
            tls: Some(TlsSettings::new("cert.pem", "key.pem")),
            ..Default::default()
        };
        assert!(matches!(config.validate(), Err(ConfigError::Invalid(_))));
    }

    #[test]
    fn alt_svc_auto_follows_the_served_port() {
        assert_eq!(
            AltSvc::Auto.header_value(HttpVersion::Http3, Some(4433)),
            Some("h3=\":4433\"".to_owned())
        );
        assert_eq!(
            AltSvc::Auto.header_value(HttpVersion::Http2, Some(4433)),
            None
        );
        assert_eq!(
            AltSvc::Off.header_value(HttpVersion::Http3, Some(4433)),
            None
        );
        assert_eq!(
            AltSvc::Custom("h3=\":443\"".to_owned()).header_value(HttpVersion::Http1, Some(4433)),
            Some("h3=\":443\"".to_owned())
        );
    }

    #[test]
    fn alt_svc_auto_is_silent_without_a_port() {
        assert_eq!(AltSvc::Auto.header_value(HttpVersion::Http3, None), None);
    }

    #[test]
    fn alpn_offers_h2_only_when_enabled() {
        assert_eq!(HttpVersion::Http1.tcp_alpn(), vec![b"http/1.1".to_vec()]);
        assert_eq!(
            HttpVersion::Http3.tcp_alpn(),
            vec![b"h2".to_vec(), b"http/1.1".to_vec()]
        );
    }

    #[test]
    fn delegated_tls_termination_hides_the_tcp_certificate() {
        let config = ServerConfig {
            tls: Some(TlsSettings {
                terminate_tcp: false,
                ..TlsSettings::new("cert.pem", "key.pem")
            }),
            ..Default::default()
        };
        assert!(config.tcp_tls().is_none());
        assert!(config.tls.is_some());
    }
}
