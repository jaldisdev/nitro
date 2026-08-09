//! TLS setup and certificate reloading.
//!
//! The certificate is reached through a resolver whose contents can be swapped
//! while the server runs. Replacing it affects handshakes from that point on
//! and leaves established sessions untouched, so a renewed certificate can be
//! picked up without dropping traffic.

use std::fs::File;
use std::io::BufReader;
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};
use std::time::SystemTime;

use rustls::pki_types::{CertificateDer, PrivateKeyDer};
use rustls::server::{ClientHello, ResolvesServerCert, WebPkiClientVerifier};
use rustls::sign::CertifiedKey;
use rustls::{RootCertStore, ServerConfig as RustlsConfig, version};
use tokio_rustls::TlsAcceptor;

use crate::config::{ClientAuth, HttpVersion, TlsSettings};
use crate::lifecycle::ShutdownSignal;

#[derive(Debug, thiserror::Error)]
pub enum TlsError {
    #[error("reading {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("{path} contains no certificates")]
    NoCertificates { path: PathBuf },
    #[error("{path} contains no private key")]
    NoPrivateKey { path: PathBuf },
    #[error("the certificate and private key do not match: {0}")]
    KeyMismatch(String),
    #[error("building the TLS configuration failed: {0}")]
    Configuration(#[from] rustls::Error),
    #[error("the client certificate authority in {path} is unusable: {reason}")]
    ClientAuthority { path: PathBuf, reason: String },
    #[error("QUIC rejected the TLS configuration: {0}")]
    Quic(String),
}

/// A certificate resolver whose active key pair can be replaced at any time.
#[derive(Debug)]
pub struct ReloadableCertificate {
    active: RwLock<Arc<CertifiedKey>>,
}

impl ReloadableCertificate {
    pub fn new(certified_key: CertifiedKey) -> Self {
        Self {
            active: RwLock::new(Arc::new(certified_key)),
        }
    }

    pub fn replace(&self, certified_key: CertifiedKey) {
        match self.active.write() {
            Ok(mut active) => *active = Arc::new(certified_key),
            // A poisoned lock means a writer panicked mid-swap. The stored value
            // is a whole `Arc` either way, so recovering it cannot expose a
            // half-written certificate.
            Err(poisoned) => *poisoned.into_inner() = Arc::new(certified_key),
        }
    }

    pub fn current(&self) -> Arc<CertifiedKey> {
        match self.active.read() {
            Ok(active) => Arc::clone(&active),
            Err(poisoned) => Arc::clone(&poisoned.into_inner()),
        }
    }
}

impl ResolvesServerCert for ReloadableCertificate {
    fn resolve(&self, _client_hello: ClientHello<'_>) -> Option<Arc<CertifiedKey>> {
        Some(self.current())
    }
}

/// Everything TLS-related that is prepared before workers exist, so a bad
/// certificate is reported once at startup rather than once per worker.
#[derive(Clone)]
pub struct TlsMaterial {
    pub settings: TlsSettings,
    pub resolver: Arc<ReloadableCertificate>,
}

impl std::fmt::Debug for TlsMaterial {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("TlsMaterial")
            .field("settings", &self.settings)
            .finish_non_exhaustive()
    }
}

impl TlsMaterial {
    pub fn load(settings: &TlsSettings) -> Result<Self, TlsError> {
        let certified_key = load_certified_key(&settings.certificate, &settings.private_key)?;
        Ok(Self {
            settings: settings.clone(),
            resolver: Arc::new(ReloadableCertificate::new(certified_key)),
        })
    }

    /// A TLS acceptor for the TCP socket, offering ALPN identifiers for every
    /// HTTP version the server will negotiate there.
    pub fn tcp_acceptor(&self, http: HttpVersion) -> Result<TlsAcceptor, TlsError> {
        let mut config = self.build_config(&[&version::TLS13, &version::TLS12])?;
        config.alpn_protocols = http.tcp_alpn();
        Ok(TlsAcceptor::from(Arc::new(config)))
    }

    /// A TLS configuration for the QUIC socket. QUIC mandates TLS 1.3 and the
    /// `h3` identifier.
    pub fn quic_config(&self) -> Result<RustlsConfig, TlsError> {
        let mut config = self.build_config(&[&version::TLS13])?;
        config.alpn_protocols = vec![b"h3".to_vec()];
        Ok(config)
    }

    fn build_config(
        &self,
        versions: &[&'static rustls::SupportedProtocolVersion],
    ) -> Result<RustlsConfig, TlsError> {
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let builder = RustlsConfig::builder_with_provider(provider.clone())
            .with_protocol_versions(versions)?;

        let resolver = Arc::clone(&self.resolver) as Arc<dyn ResolvesServerCert>;
        let config = match &self.settings.client_auth {
            ClientAuth::None => builder.with_no_client_auth().with_cert_resolver(resolver),
            ClientAuth::Optional(authority) => builder
                .with_client_cert_verifier(client_verifier(authority, &provider, true)?)
                .with_cert_resolver(resolver),
            ClientAuth::Required(authority) => builder
                .with_client_cert_verifier(client_verifier(authority, &provider, false)?)
                .with_cert_resolver(resolver),
        };
        Ok(config)
    }

    /// Poll the certificate file and swap the resolver's contents when it
    /// changes. Returns `None` when reloading is switched off.
    ///
    /// Polling beats watching the filesystem here because certificate renewal
    /// often replaces the file by rename or recreates it, which invalidates a
    /// watch on the original inode.
    pub fn spawn_reloader(&self, shutdown: ShutdownSignal) -> Option<tokio::task::JoinHandle<()>> {
        if self.settings.reload_interval.is_zero() {
            return None;
        }
        let material = self.clone();
        Some(tokio::spawn(
            async move { material.reload_loop(shutdown).await },
        ))
    }

    async fn reload_loop(self, shutdown: ShutdownSignal) {
        let mut last_modified = modified_at(&self.settings.certificate);
        let mut ticker = tokio::time::interval(self.settings.reload_interval);
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

        loop {
            tokio::select! {
                _ = ticker.tick() => {}
                // A loop with no exit is awaited by the drain chain like any
                // other background task, so one that never returns holds
                // shutdown open for the whole drain timeout. There is nothing
                // to reload for once the server is stopping anyway.
                () = shutdown.wait() => return,
            }

            let modified = modified_at(&self.settings.certificate);
            if modified <= last_modified {
                continue;
            }
            last_modified = modified;

            match load_certified_key(&self.settings.certificate, &self.settings.private_key) {
                Ok(certified_key) => {
                    self.resolver.replace(certified_key);
                    tracing::info!(
                        certificate = ?self.settings.certificate,
                        "TLS certificate reloaded"
                    );
                }
                // A renewal caught mid-write leaves an unusable file for a
                // moment. Keeping the previous key is the safe reaction; the
                // next tick picks up the finished file.
                Err(error) => tracing::error!(
                    certificate = ?self.settings.certificate,
                    %error,
                    "keeping the previous TLS certificate"
                ),
            }
        }
    }
}

fn modified_at(path: &Path) -> SystemTime {
    std::fs::metadata(path)
        .and_then(|metadata| metadata.modified())
        .unwrap_or(SystemTime::UNIX_EPOCH)
}

pub fn load_certified_key(
    certificate: &Path,
    private_key: &Path,
) -> Result<CertifiedKey, TlsError> {
    let certificates = load_certificates(certificate)?;
    let key = load_private_key(private_key)?;
    let signing_key = rustls::crypto::ring::sign::any_supported_type(&key)
        .map_err(|error| TlsError::KeyMismatch(error.to_string()))?;

    let certified_key = CertifiedKey::new(certificates, signing_key);
    certified_key
        .keys_match()
        .map_err(|error| TlsError::KeyMismatch(error.to_string()))?;
    Ok(certified_key)
}

fn load_certificates(path: &Path) -> Result<Vec<CertificateDer<'static>>, TlsError> {
    let file = File::open(path).map_err(|source| TlsError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    let certificates = rustls_pemfile::certs(&mut BufReader::new(file))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|source| TlsError::Io {
            path: path.to_path_buf(),
            source,
        })?;

    if certificates.is_empty() {
        return Err(TlsError::NoCertificates {
            path: path.to_path_buf(),
        });
    }
    Ok(certificates)
}

fn load_private_key(path: &Path) -> Result<PrivateKeyDer<'static>, TlsError> {
    let file = File::open(path).map_err(|source| TlsError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    rustls_pemfile::private_key(&mut BufReader::new(file))
        .map_err(|source| TlsError::Io {
            path: path.to_path_buf(),
            source,
        })?
        .ok_or_else(|| TlsError::NoPrivateKey {
            path: path.to_path_buf(),
        })
}

fn client_verifier(
    authority: &Path,
    provider: &Arc<rustls::crypto::CryptoProvider>,
    allow_anonymous: bool,
) -> Result<Arc<dyn rustls::server::danger::ClientCertVerifier>, TlsError> {
    let mut roots = RootCertStore::empty();
    for certificate in load_certificates(authority)? {
        roots
            .add(certificate)
            .map_err(|error| TlsError::ClientAuthority {
                path: authority.to_path_buf(),
                reason: error.to_string(),
            })?;
    }

    let builder =
        WebPkiClientVerifier::builder_with_provider(Arc::new(roots), Arc::clone(provider));
    let builder = if allow_anonymous {
        builder.allow_unauthenticated()
    } else {
        builder
    };
    builder.build().map_err(|error| TlsError::ClientAuthority {
        path: authority.to_path_buf(),
        reason: error.to_string(),
    })
}

#[cfg(test)]
pub(crate) mod test_support {
    use std::path::Path;

    /// Write a self-signed certificate and key for `hostname` into `directory`.
    pub fn write_self_signed(
        directory: &Path,
        hostname: &str,
    ) -> (std::path::PathBuf, std::path::PathBuf) {
        let generated = rcgen::generate_simple_self_signed([hostname.to_owned()])
            .expect("generating a test certificate must succeed");
        let certificate_path = directory.join(format!("{hostname}.pem"));
        let key_path = directory.join(format!("{hostname}.key"));
        std::fs::write(&certificate_path, generated.cert.pem()).expect("writing the certificate");
        std::fs::write(&key_path, generated.signing_key.serialize_pem()).expect("writing the key");
        (certificate_path, key_path)
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::test_support::write_self_signed;
    use super::*;
    use crate::lifecycle::ShutdownController;

    fn settings(directory: &Path, hostname: &str) -> TlsSettings {
        let (certificate, key) = write_self_signed(directory, hostname);
        TlsSettings::new(certificate, key)
    }

    #[test]
    fn a_valid_pair_loads() {
        let directory = tempfile::tempdir().unwrap();
        let material = TlsMaterial::load(&settings(directory.path(), "first.test")).unwrap();
        assert!(!material.resolver.current().cert.is_empty());
    }

    #[test]
    fn a_mismatched_pair_is_rejected() {
        let directory = tempfile::tempdir().unwrap();
        let (certificate, _) = write_self_signed(directory.path(), "first.test");
        let (_, other_key) = write_self_signed(directory.path(), "second.test");

        let error = TlsMaterial::load(&TlsSettings::new(certificate, other_key))
            .expect_err("a key from a different certificate must not be accepted");
        assert!(matches!(error, TlsError::KeyMismatch(_)));
    }

    #[test]
    fn a_missing_certificate_names_the_path() {
        let error = TlsMaterial::load(&TlsSettings::new(
            "/nonexistent/cert.pem",
            "/nonexistent/key.pem",
        ))
        .expect_err("a missing file must be an error");
        assert!(matches!(error, TlsError::Io { .. }));
        assert!(error.to_string().contains("/nonexistent/cert.pem"));
    }

    #[test]
    fn a_file_without_certificates_is_rejected() {
        let directory = tempfile::tempdir().unwrap();
        let empty = directory.path().join("empty.pem");
        std::fs::write(&empty, b"not a certificate\n").unwrap();
        let (_, key) = write_self_signed(directory.path(), "first.test");

        let error = TlsMaterial::load(&TlsSettings::new(&empty, key))
            .expect_err("a file with no PEM blocks must be an error");
        assert!(matches!(error, TlsError::NoCertificates { .. }));
    }

    #[test]
    fn alpn_follows_the_negotiated_http_version() {
        let directory = tempfile::tempdir().unwrap();
        let material = TlsMaterial::load(&settings(directory.path(), "first.test")).unwrap();

        assert!(material.tcp_acceptor(HttpVersion::Http1).is_ok());
        assert!(material.tcp_acceptor(HttpVersion::Http3).is_ok());
        assert_eq!(
            material.quic_config().unwrap().alpn_protocols,
            vec![b"h3".to_vec()]
        );
    }

    #[test]
    fn replacing_the_certificate_changes_what_the_resolver_hands_out() {
        let directory = tempfile::tempdir().unwrap();
        let material = TlsMaterial::load(&settings(directory.path(), "first.test")).unwrap();
        let before = material.resolver.current();

        let (certificate, key) = write_self_signed(directory.path(), "second.test");
        material
            .resolver
            .replace(load_certified_key(&certificate, &key).unwrap());

        assert_ne!(
            before.cert[0].as_ref(),
            material.resolver.current().cert[0].as_ref()
        );
    }

    #[tokio::test]
    async fn the_reloader_picks_up_a_replaced_certificate() {
        let directory = tempfile::tempdir().unwrap();
        let mut settings = settings(directory.path(), "first.test");
        settings.reload_interval = Duration::from_millis(20);

        let material = TlsMaterial::load(&settings).unwrap();
        let before = material.resolver.current();
        let reloader = material
            .spawn_reloader(ShutdownSignal::never())
            .expect("reloading is enabled");

        // The poller compares modification times, which on some filesystems have
        // a resolution coarse enough to miss an immediate rewrite.
        tokio::time::sleep(Duration::from_millis(50)).await;
        let replacement = rcgen::generate_simple_self_signed(["renewed.test".to_owned()]).unwrap();
        std::fs::write(&settings.certificate, replacement.cert.pem()).unwrap();
        std::fs::write(
            &settings.private_key,
            replacement.signing_key.serialize_pem(),
        )
        .unwrap();

        let mut reloaded = false;
        for _ in 0..50 {
            tokio::time::sleep(Duration::from_millis(20)).await;
            if material.resolver.current().cert[0].as_ref() != before.cert[0].as_ref() {
                reloaded = true;
                break;
            }
        }
        reloader.abort();
        assert!(
            reloaded,
            "the reloader must swap in the renewed certificate"
        );
    }

    #[tokio::test]
    async fn the_reloader_stops_when_shutdown_is_requested() {
        let directory = tempfile::tempdir().unwrap();
        let mut settings = settings(directory.path(), "first.test");
        // Far longer than the assertion below waits, so a reloader that only
        // noticed the request on its next tick would fail this.
        settings.reload_interval = Duration::from_secs(300);

        let material = TlsMaterial::load(&settings).unwrap();
        let controller = ShutdownController::new();
        let reloader = material
            .spawn_reloader(controller.subscribe())
            .expect("reloading is enabled");

        controller.trigger();

        tokio::time::timeout(Duration::from_secs(1), reloader)
            .await
            .expect("the reloader must return rather than hold the drain open")
            .expect("the reloader task panicked");
    }

    #[tokio::test]
    async fn an_unreadable_replacement_keeps_the_previous_certificate() {
        let directory = tempfile::tempdir().unwrap();
        let mut settings = settings(directory.path(), "first.test");
        settings.reload_interval = Duration::from_millis(20);

        let material = TlsMaterial::load(&settings).unwrap();
        let before = material.resolver.current();
        let reloader = material
            .spawn_reloader(ShutdownSignal::never())
            .expect("reloading is enabled");

        tokio::time::sleep(Duration::from_millis(50)).await;
        std::fs::write(
            &settings.certificate,
            b"-----BEGIN CERTIFICATE-----\ntruncated\n",
        )
        .unwrap();
        tokio::time::sleep(Duration::from_millis(150)).await;
        reloader.abort();

        assert_eq!(
            before.cert[0].as_ref(),
            material.resolver.current().cert[0].as_ref(),
            "a half-written certificate must not replace a working one"
        );
    }

    #[test]
    fn reloading_can_be_switched_off() {
        let directory = tempfile::tempdir().unwrap();
        let mut settings = settings(directory.path(), "first.test");
        settings.reload_interval = Duration::ZERO;

        let material = TlsMaterial::load(&settings).unwrap();
        assert!(material.spawn_reloader(ShutdownSignal::never()).is_none());
    }
}
