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

//! Prometheus metrics for the Nitro server.
//!
//! Split out from the transport so that other parts of the system can report
//! metrics without depending on the server's accept loops and lifecycle.
//!
//! Metrics are always collected — an increment on a counter costs an atomic
//! operation whether or not anyone is looking. What the configuration decides
//! is only whether a listener exists for a scraper to read from.

pub mod exporter;
pub mod metrics;

pub use exporter::{
    BoundExporter, DEFAULT_HOST, DEFAULT_PORT, ExporterConfig, ExporterError, METRICS_PATH,
};
pub use metrics::{
    SocketProtocol, Transport, connection_closed, connection_opened, record_request, render,
    request_finished, request_started, socket_closed, socket_handshake, worker_draining,
    worker_started,
};
