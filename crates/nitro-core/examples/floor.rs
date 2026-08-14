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

//! Nitro's transport with no Python behind it, as a floor to measure against.
//!
//! Everything the server does for a request happens here — accept, parse,
//! route-free dispatch, write — except crossing into the interpreter. Comparing
//! this against the same request served by an application says what that
//! crossing costs, which is otherwise guesswork: the cheapest possible Python
//! handler still pays for a scope, a coroutine, a task and a trip through the
//! event loop, so no measurement taken through one can isolate the transport.
//!
//! Run: `cargo run --release --example floor -- 8300`

use std::sync::Arc;

use http::StatusCode;
use nitro_core::config::{BindAddress, HttpVersion, ServerConfig};
use nitro_core::headers::Headers;
use nitro_core::lifecycle::drain::DrainCoordinator;
use nitro_core::lifecycle::signals::ShutdownController;
use nitro_core::transport::accept::{BoundSockets, serve};
use nitro_core::transport::{Dispatch, HttpRequest, HttpResponse, ResponseBody};

#[derive(Clone)]
struct Fixed {
    body: bytes::Bytes,
}

impl Dispatch for Fixed {
    async fn handle_http(&self, _request: HttpRequest) -> HttpResponse {
        let mut headers = Headers::new();
        let _ = headers.insert("content-type", "text/plain; charset=utf-8");
        HttpResponse {
            status: StatusCode::OK,
            headers,
            body: ResponseBody::Bytes(self.body.clone()),
            route: None,
        }
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let port: u16 = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "8300".to_owned())
        .parse()?;

    let listener = std::net::TcpListener::bind(("127.0.0.1", port))?;
    listener.set_nonblocking(true)?;

    let mut config = ServerConfig {
        bind: BindAddress::Tcp {
            host: "127.0.0.1".to_owned(),
            port,
        },
        http: HttpVersion::Http1,
        ..ServerConfig::default()
    };
    config.websockets = false;
    config.webtransport = false;

    let sockets = BoundSockets {
        tcp: vec![listener],
        #[cfg(unix)]
        unix: None,
        quic: Vec::new(),
        metrics: Vec::new(),
    };

    // `st` builds a current-thread runtime, the shape Granian serves its
    // single-threaded mode on; the default is what Nitro's server uses today.
    let single_threaded = std::env::args().nth(2).as_deref() == Some("st");
    let runtime = if single_threaded {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?
    } else {
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()?
    };

    let controller = ShutdownController::new();
    let shutdown = controller.subscribe();
    let drain = DrainCoordinator::new(config.drain_timeout);
    let dispatch = Fixed {
        body: bytes::Bytes::from_static(b"xxxxxxxxxx"),
    };

    println!("Serving on http://127.0.0.1:{port}");
    runtime.block_on(async move {
        serve(sockets, dispatch, Arc::new(config), None, shutdown, drain).await
    })?;
    Ok(())
}
