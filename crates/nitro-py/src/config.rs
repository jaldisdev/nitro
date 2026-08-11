//
// This source file is part of the Nitro open source project.
//
// Copyright (c) 2026 Jaldis B.V.
//
// Licensed under the MIT OR Apache-2.0 license (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://opensource.org/licenses/MIT
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//

//! Turning a Python settings object into the core configuration structures.
//!
//! Values are read by attribute, falling back to mapping lookup, so both a
//! settings object and a plain dictionary work. Every field is optional and
//! keeps its default when absent, which means adding a setting here does not
//! break a caller that predates it.

use std::path::PathBuf;
use std::time::Duration;

use nitro_core::config::{
    AccessLogConfig, AccessLogFormat, AltSvc, BindAddress, ClientAuth, HttpVersion, LogDestination,
    LogFormat, LogLevel, LoggingConfig, ServerConfig, TlsSettings,
};
use nitro_core::hosts::AllowedHosts;
use nitro_core::observability::ExporterConfig;
use pyo3::exceptions::{PyAttributeError, PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyMapping;

/// Read `name` from an object's attributes, then from it as a mapping.
///
/// A value of `None` counts as absent so a caller can spell "use the default"
/// either way.
///
/// Only "there is no such name" counts as absent. A settings object whose
/// property *raised* is a broken configuration, and treating that as absent
/// would start a server on defaults while its settings module says otherwise —
/// the failure would then show up as behaviour nobody asked for rather than as
/// the error it is.
fn lookup<'py>(source: &Bound<'py, PyAny>, name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    let python = source.py();

    let found = match source.getattr(name) {
        Ok(value) => Some(value),
        Err(error) if error.is_instance_of::<PyAttributeError>(python) => {
            // Not an attribute. A plain dictionary is accepted too, so that a
            // caller can pass one instead of a settings object.
            match source.cast::<PyMapping>() {
                Ok(mapping) => match mapping.get_item(name) {
                    Ok(value) => Some(value),
                    Err(error) if error.is_instance_of::<PyKeyError>(python) => None,
                    Err(error) => return Err(error),
                },
                Err(_) => None,
            }
        }
        Err(error) => return Err(error),
    };

    Ok(found.filter(|value| !value.is_none()))
}

fn extract<'py, T>(source: &Bound<'py, PyAny>, name: &str, fallback: T) -> PyResult<T>
where
    T: for<'a> FromPyObject<'a, 'py, Error = PyErr>,
{
    match lookup(source, name)? {
        Some(value) => value.extract().map_err(|error: PyErr| {
            PyValueError::new_err(format!("setting '{name}' is not usable: {error}"))
        }),
        None => Ok(fallback),
    }
}

fn extract_optional<'py, T>(source: &Bound<'py, PyAny>, name: &str) -> PyResult<Option<T>>
where
    T: for<'a> FromPyObject<'a, 'py, Error = PyErr>,
{
    match lookup(source, name)? {
        Some(value) => value.extract().map(Some).map_err(|error: PyErr| {
            PyValueError::new_err(format!("setting '{name}' is not usable: {error}"))
        }),
        None => Ok(None),
    }
}

fn parse_http_version(value: &str) -> PyResult<HttpVersion> {
    match value {
        "1" | "1.1" => Ok(HttpVersion::Http1),
        "2" => Ok(HttpVersion::Http2),
        "3" | "auto" => Ok(HttpVersion::Http3),
        other => Err(PyValueError::new_err(format!(
            "setting 'http' must be one of 'auto', '1', '2' or '3', got {other:?}"
        ))),
    }
}

fn parse_log_level(value: &str) -> PyResult<LogLevel> {
    match value {
        "trace" => Ok(LogLevel::Trace),
        "debug" => Ok(LogLevel::Debug),
        "info" => Ok(LogLevel::Info),
        "warning" | "warn" => Ok(LogLevel::Warning),
        "error" => Ok(LogLevel::Error),
        other => Err(PyValueError::new_err(format!(
            "setting 'log_level' must be a level name, got {other:?}"
        ))),
    }
}

/// `stdout` and `stderr` name the streams; anything else is a file path.
fn parse_log_destination(value: &str) -> LogDestination {
    match value {
        "stdout" => LogDestination::Stdout,
        "stderr" => LogDestination::Stderr,
        path => LogDestination::File(PathBuf::from(path)),
    }
}

fn parse_log_format(name: &str, value: &str) -> PyResult<LogFormat> {
    match value {
        "pretty" => Ok(LogFormat::Pretty),
        "json" => Ok(LogFormat::Json),
        other => Err(PyValueError::new_err(format!(
            "setting '{name}' must be 'pretty' or 'json', got {other:?}"
        ))),
    }
}

fn parse_access_log_format(value: &str) -> PyResult<AccessLogFormat> {
    match value {
        "combined" => Ok(AccessLogFormat::Combined),
        "json" => Ok(AccessLogFormat::Json),
        other => Err(PyValueError::new_err(format!(
            "setting 'access_log_format' must be 'combined' or 'json', got {other:?}"
        ))),
    }
}

fn parse_client_auth(mode: &str, authority: Option<PathBuf>) -> PyResult<ClientAuth> {
    match mode {
        "none" => Ok(ClientAuth::None),
        "optional" | "required" => {
            let authority = authority.ok_or_else(|| {
                PyValueError::new_err(
                    "setting 'tls_ca' is required when client certificates are verified",
                )
            })?;
            if mode == "optional" {
                Ok(ClientAuth::Optional(authority))
            } else {
                Ok(ClientAuth::Required(authority))
            }
        }
        other => Err(PyValueError::new_err(format!(
            "setting 'tls_client_auth' must be 'none', 'optional' or 'required', got {other:?}"
        ))),
    }
}

fn tls_from(source: &Bound<'_, PyAny>) -> PyResult<Option<TlsSettings>> {
    let certificate: Option<PathBuf> = extract_optional(source, "tls_cert")?;
    let private_key: Option<PathBuf> = extract_optional(source, "tls_key")?;

    match (certificate, private_key) {
        (None, None) => Ok(None),
        (Some(certificate), Some(private_key)) => {
            let authority: Option<PathBuf> = extract_optional(source, "tls_ca")?;
            let mode: String = extract(source, "tls_client_auth", "none".to_owned())?;
            let reload_seconds: f64 = extract(source, "tls_reload_interval", 10.0)?;

            Ok(Some(TlsSettings {
                certificate,
                private_key,
                client_auth: parse_client_auth(&mode, authority)?,
                terminate_tcp: extract(source, "tls_tcp", true)?,
                reload_interval: Duration::from_secs_f64(reload_seconds.max(0.0)),
            }))
        }
        _ => Err(PyValueError::new_err(
            "settings 'tls_cert' and 'tls_key' must be given together",
        )),
    }
}

fn access_log_from(source: &Bound<'_, PyAny>) -> PyResult<Option<AccessLogConfig>> {
    if !extract(source, "access_log", false)? {
        return Ok(None);
    }
    let destination: String = extract(source, "access_log_destination", "stdout".to_owned())?;
    let format: String = extract(source, "access_log_format", "combined".to_owned())?;

    Ok(Some(AccessLogConfig {
        destination: parse_log_destination(&destination),
        format: parse_access_log_format(&format)?,
    }))
}

fn bind_from(source: &Bound<'_, PyAny>) -> PyResult<BindAddress> {
    match extract_optional::<PathBuf>(source, "uds")? {
        Some(path) => Ok(BindAddress::Unix { path }),
        None => Ok(BindAddress::tcp(
            extract(source, "host", "localhost".to_owned())?,
            extract(source, "port", 8000_u16)?,
        )),
    }
}

fn alt_svc_from(source: &Bound<'_, PyAny>) -> PyResult<AltSvc> {
    let value: String = extract(source, "alt_svc", "auto".to_owned())?;
    Ok(match value.as_str() {
        "auto" => AltSvc::Auto,
        "off" => AltSvc::Off,
        verbatim => AltSvc::Custom(verbatim.to_owned()),
    })
}

/// Read the flat `observability_*` options.
///
/// Flat rather than nested because there is exactly one exporter; a mapping
/// would suggest several named ones can be configured.
fn observability_from(
    source: &Bound<'_, PyAny>,
    defaults: &ExporterConfig,
) -> PyResult<ExporterConfig> {
    let host: String = extract(source, "observability_host", defaults.host.clone())?;
    let port: u16 = extract(source, "observability_port", defaults.port)?;

    if host.trim().is_empty() {
        return Err(PyValueError::new_err(
            "observability_host must not be empty",
        ));
    }

    Ok(ExporterConfig {
        enabled: extract(source, "observability_enabled", defaults.enabled)?,
        host,
        port,
    })
}

/// Build a validated [`ServerConfig`] from a Python settings object.
pub fn server_config(source: &Bound<'_, PyAny>) -> PyResult<ServerConfig> {
    let defaults = ServerConfig::default();
    let log_level: String = extract(source, "log_level", "info".to_owned())?;
    let log_destination: String = extract(source, "log_destination", "stderr".to_owned())?;
    let log_format: String = extract(source, "log_format", "pretty".to_owned())?;
    let drain_seconds: f64 = extract(source, "drain_timeout", 30.0)?;

    let mut config = ServerConfig {
        bind: bind_from(source)?,
        allowed_hosts: AllowedHosts::new(extract::<Vec<String>>(
            source,
            "allowed_hosts",
            Vec::new(),
        )?),
        observability: observability_from(source, &defaults.observability)?,
        tls: tls_from(source)?,
        http: parse_http_version(&extract(source, "http", "auto".to_owned())?)?,
        websockets: extract(source, "websockets", defaults.websockets)?,
        webtransport: extract(source, "webtransport", defaults.webtransport)?,
        workers: extract(source, "workers", defaults.workers)?,
        runtime_threads: extract(source, "runtime_threads", defaults.runtime_threads)?,
        backlog: extract(source, "backlog", defaults.backlog)?,
        max_concurrent_connections: extract_optional(source, "max_concurrent_connections")?,
        datagram_queue_capacity: extract(
            source,
            "datagram_queue_capacity",
            defaults.datagram_queue_capacity,
        )?,
        stream_queue_capacity: extract(
            source,
            "stream_queue_capacity",
            defaults.stream_queue_capacity,
        )?,
        alt_svc: alt_svc_from(source)?,
        drain_timeout: Duration::from_secs_f64(drain_seconds.max(0.0)),
        server_header: match lookup(source, "server_header")? {
            Some(value) => Some(value.extract()?),
            None => defaults.server_header,
        },
        logging: LoggingConfig {
            level: parse_log_level(&log_level)?,
            destination: parse_log_destination(&log_destination),
            format: parse_log_format("log_format", &log_format)?,
        },
        access_log: access_log_from(source)?,
    };

    config
        .validate()
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    Ok(config)
}
