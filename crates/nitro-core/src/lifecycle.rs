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

//! Startup, signal handling, graceful shutdown and logging.

pub mod drain;
pub mod signals;

use std::fs::OpenOptions;
use std::io::{self, Write};
use std::net::SocketAddr;
use std::path::Path;
use std::sync::Mutex;
use std::time::{Duration, SystemTime};

use time::format_description::BorrowedFormatItem;
use time::macros::format_description;
use tracing_subscriber::fmt::writer::BoxMakeWriter;

use crate::config::{AccessLogConfig, AccessLogFormat, LogDestination, LogFormat, LoggingConfig};

pub use drain::{DrainCoordinator, DrainOutcome};
pub use signals::{ShutdownController, ShutdownSignal};

/// Timestamp layout for the common log format: `10/Aug/2026:14:03:21 +0000`.
const ACCESS_LOG_TIMESTAMP: &[BorrowedFormatItem<'_>] =
    format_description!("[day]/[month repr:short]/[year]:[hour]:[minute]:[second] +0000");

#[derive(Debug, thiserror::Error)]
pub enum LoggingError {
    #[error("opening the log file {path}: {source}")]
    OpenFile {
        path: std::path::PathBuf,
        #[source]
        source: io::Error,
    },
}

/// Install the process-wide log subscriber.
///
/// Called once per worker. A second call is a no-op rather than an error,
/// because an embedding process may already have installed its own subscriber
/// and taking it over would silently redirect its logs.
pub fn init_tracing(config: &LoggingConfig) -> Result<(), LoggingError> {
    let writer = make_writer(&config.destination)?;
    let filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new(config.level.as_filter()));

    let builder = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_writer(writer);

    let installed = match config.format {
        LogFormat::Pretty => builder.try_init().is_ok(),
        LogFormat::Json => builder.json().try_init().is_ok(),
    };
    if !installed {
        tracing::debug!("a log subscriber was already installed; leaving it in place");
    }
    Ok(())
}

fn make_writer(destination: &LogDestination) -> Result<BoxMakeWriter, LoggingError> {
    Ok(match destination {
        LogDestination::Stdout => BoxMakeWriter::new(io::stdout),
        LogDestination::Stderr => BoxMakeWriter::new(io::stderr),
        LogDestination::File(path) => BoxMakeWriter::new(Mutex::new(open_append(path)?)),
    })
}

fn open_append(path: &Path) -> Result<std::fs::File, LoggingError> {
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|source| LoggingError::OpenFile {
            path: path.to_path_buf(),
            source,
        })
}

/// One completed request, as the access log sees it.
#[derive(Debug, Clone, Copy)]
pub struct AccessRecord<'a> {
    pub client: Option<SocketAddr>,
    pub method: &'a str,
    pub target: &'a str,
    pub http_version: &'a str,
    pub status: u16,
    pub body_length: Option<u64>,
    pub referer: Option<&'a str>,
    pub user_agent: Option<&'a str>,
    pub duration: Duration,
}

/// Writes access log lines, separately from the server log so the two can go to
/// different places in different formats.
#[derive(Debug)]
pub struct AccessLogger {
    sink: Sink,
    format: AccessLogFormat,
}

#[derive(Debug)]
enum Sink {
    Stdout,
    Stderr,
    File(Mutex<std::fs::File>),
}

impl AccessLogger {
    /// A logger for `config`. A log file that cannot be opened falls back to
    /// standard error rather than preventing the server from starting: losing
    /// access records is bad, refusing to serve traffic over it is worse.
    pub fn new(config: &AccessLogConfig) -> Self {
        let sink = match &config.destination {
            LogDestination::Stdout => Sink::Stdout,
            LogDestination::Stderr => Sink::Stderr,
            LogDestination::File(path) => match open_append(path) {
                Ok(file) => Sink::File(Mutex::new(file)),
                Err(error) => {
                    tracing::error!(%error, "writing access logs to stderr instead");
                    Sink::Stderr
                }
            },
        };
        Self {
            sink,
            format: config.format,
        }
    }

    pub fn record(&self, record: AccessRecord<'_>) {
        let line = match self.format {
            AccessLogFormat::Combined => format_combined(&record),
            AccessLogFormat::Json => format_json(&record),
        };
        self.write(&line);
    }

    fn write(&self, line: &str) {
        let result = match &self.sink {
            Sink::Stdout => writeln!(io::stdout().lock(), "{line}"),
            Sink::Stderr => writeln!(io::stderr().lock(), "{line}"),
            Sink::File(file) => match file.lock() {
                Ok(mut file) => writeln!(file, "{line}"),
                Err(poisoned) => writeln!(poisoned.into_inner(), "{line}"),
            },
        };
        if let Err(error) = result {
            tracing::warn!(%error, "could not write an access log entry");
        }
    }
}

fn client_field(record: &AccessRecord<'_>) -> String {
    record
        .client
        .map(|address| address.ip().to_string())
        .unwrap_or_else(|| "-".to_owned())
}

fn format_combined(record: &AccessRecord<'_>) -> String {
    let timestamp = time::OffsetDateTime::from(SystemTime::now())
        .format(ACCESS_LOG_TIMESTAMP)
        .unwrap_or_else(|_| "-".to_owned());
    let length = record
        .body_length
        .map(|length| length.to_string())
        .unwrap_or_else(|| "-".to_owned());

    format!(
        "{client} - - [{timestamp}] \"{method} {target} HTTP/{version}\" {status} {length} \"{referer}\" \"{agent}\"",
        client = client_field(record),
        method = record.method,
        target = record.target,
        version = record.http_version,
        status = record.status,
        referer = record.referer.unwrap_or("-"),
        agent = record.user_agent.unwrap_or("-"),
    )
}

fn format_json(record: &AccessRecord<'_>) -> String {
    let value = serde_json::json!({
        "client": record.client.map(|address| address.ip().to_string()),
        "method": record.method,
        "target": record.target,
        "http_version": record.http_version,
        "status": record.status,
        "body_length": record.body_length,
        "referer": record.referer,
        "user_agent": record.user_agent,
        "duration_ms": record.duration.as_secs_f64() * 1000.0,
    });
    value.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record() -> AccessRecord<'static> {
        AccessRecord {
            client: Some("203.0.113.7:51234".parse().unwrap()),
            method: "GET",
            target: "/index.html?page=2",
            http_version: "1.1",
            status: 200,
            body_length: Some(1234),
            referer: Some("https://example.test/"),
            user_agent: Some("probe/1.0"),
            duration: Duration::from_millis(12),
        }
    }

    #[test]
    fn a_combined_line_carries_every_field() {
        let line = format_combined(&record());
        assert!(line.starts_with("203.0.113.7 - - ["));
        assert!(line.contains("\"GET /index.html?page=2 HTTP/1.1\" 200 1234"));
        assert!(line.ends_with("\"https://example.test/\" \"probe/1.0\""));
    }

    #[test]
    fn missing_fields_become_dashes() {
        let line = format_combined(&AccessRecord {
            client: None,
            body_length: None,
            referer: None,
            user_agent: None,
            ..record()
        });
        assert!(line.starts_with("- - - ["));
        assert!(line.contains("HTTP/1.1\" 200 -"));
        assert!(line.ends_with("\"-\" \"-\""));
    }

    #[test]
    fn a_json_line_parses_back_to_the_same_values() {
        let parsed: serde_json::Value = serde_json::from_str(&format_json(&record())).unwrap();
        assert_eq!(parsed["client"], "203.0.113.7");
        assert_eq!(parsed["method"], "GET");
        assert_eq!(parsed["status"], 200);
        assert_eq!(parsed["body_length"], 1234);
        assert!(parsed["duration_ms"].as_f64().unwrap() >= 12.0);
    }

    #[test]
    fn json_absent_fields_are_null_rather_than_missing() {
        let line = format_json(&AccessRecord {
            client: None,
            body_length: None,
            referer: None,
            user_agent: None,
            ..record()
        });
        let parsed: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert!(parsed["client"].is_null());
        assert!(parsed["referer"].is_null());
        assert!(parsed.get("user_agent").is_some());
    }

    #[test]
    fn a_target_with_quotes_stays_parseable_as_json() {
        let parsed: serde_json::Value = serde_json::from_str(&format_json(&AccessRecord {
            target: "/search?q=\"quoted\"",
            ..record()
        }))
        .expect("quoting must be escaped");
        assert_eq!(parsed["target"], "/search?q=\"quoted\"");
    }

    #[test]
    fn entries_reach_a_file_sink() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("access.log");
        let logger = AccessLogger::new(&AccessLogConfig {
            destination: LogDestination::File(path.clone()),
            format: AccessLogFormat::Combined,
        });

        logger.record(record());
        logger.record(record());

        let written = std::fs::read_to_string(&path).unwrap();
        assert_eq!(written.lines().count(), 2);
        assert!(written.contains("GET /index.html?page=2"));
    }

    #[test]
    fn an_unopenable_log_file_falls_back_instead_of_failing() {
        let logger = AccessLogger::new(&AccessLogConfig {
            destination: LogDestination::File("/nonexistent-directory/access.log".into()),
            format: AccessLogFormat::Combined,
        });
        assert!(matches!(logger.sink, Sink::Stderr));
        logger.record(record());
    }
}
