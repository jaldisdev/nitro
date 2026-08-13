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

//! Handing a request to the Python application and collecting its answer.

use std::sync::Arc;

use http::StatusCode;
use nitro_core::files::{OpenFile, ResolvedRange, resolve_range};
use nitro_core::headers::Headers;
use nitro_core::router::{RouteMatch, RouteTable};
use nitro_core::transport::{Dispatch, HttpRequest, HttpResponse, ResponseBody, WebSocketRequest};
use nitro_core::webtransport::WebTransportRequest;
use pyo3::prelude::*;
use pyo3_async_runtimes::TaskLocals;
use tokio::sync::oneshot;

use crate::protocol::{
    FileRequest, HttpProtocol, PreparedBody, PreparedResponse, WsTransport, WtSession,
};
use crate::scope::{HttpScope, WsScope, WtScope};

/// The name a Nitro application exposes to answer HTTP requests.
pub const HTTP_ENTRY_POINT: &str = "__handle_http__";

/// The name a Nitro application exposes to answer WebSocket upgrades.
pub const WEBSOCKET_ENTRY_POINT: &str = "__handle_ws__";

/// The name a Nitro application exposes to answer WebTransport sessions.
pub const WEBTRANSPORT_ENTRY_POINT: &str = "__handle_wt__";

/// The pseudo-methods WebSocket and WebTransport routes are registered under,
/// so one route table covers every protocol.
pub const WEBSOCKET_METHOD: &str = "WEBSOCKET";
pub const WEBTRANSPORT_METHOD: &str = "WEBTRANSPORT";

/// Calls into a Python application object.
///
/// The response travels back over a one-shot channel rather than being read out
/// of shared state once the handler returns. That is what lets a handler send
/// its response and keep working — a streaming body, for instance, is still
/// being produced long after the response itself has gone out.
#[derive(Clone)]
pub struct PythonDispatch {
    application: Arc<Py<PyAny>>,
    routes: Arc<RouteTable>,
    locals: TaskLocals,
    stream_capacity: usize,
}

impl PythonDispatch {
    pub fn new(
        application: Py<PyAny>,
        routes: Arc<RouteTable>,
        locals: TaskLocals,
        stream_capacity: usize,
    ) -> Self {
        Self {
            application: Arc::new(application),
            routes,
            locals,
            stream_capacity,
        }
    }

    /// Start the handler coroutine, returning a channel the response arrives on.
    fn start_handler(
        &self,
        request: HttpRequest,
    ) -> PyResult<(
        oneshot::Receiver<PreparedResponse>,
        impl Future<Output = ()> + Send + use<>,
        Option<String>,
    )> {
        let (responder, receiver) = oneshot::channel();

        // Matching happens before the handler is built so the scope can carry
        // the route and its captured parameters, and so an application never
        // has to work out which route a path belongs to.
        let matched = self
            .routes
            .find(request.parts.method.as_str(), request.parts.path());
        let route = match &matched {
            RouteMatch::Found { route_id, .. } => {
                self.routes.declared_path(*route_id).map(str::to_owned)
            }
            _ => None,
        };

        let HttpRequest {
            parts,
            body,
            disconnect,
        } = request;

        let coroutine = Python::attach(|python| -> PyResult<_> {
            let scope = Py::new(python, HttpScope::from_parts(python, parts, &matched)?)?;
            let protocol = Py::new(
                python,
                HttpProtocol::new(
                    body,
                    responder,
                    disconnect,
                    self.stream_capacity,
                ),
            )?;

            let application = self.application.bind(python);
            let entry_point = if application.hasattr(HTTP_ENTRY_POINT)? {
                application.getattr(HTTP_ENTRY_POINT)?
            } else {
                application.clone()
            };

            let awaitable = entry_point.call1((scope, protocol))?;
            pyo3_async_runtimes::into_future_with_locals(&self.locals, awaitable)
        })?;

        Ok((
            receiver,
            async move {
                if let Err(error) = coroutine.await {
                    Python::attach(|python| {
                        tracing::error!(error = %error.value(python), "the HTTP handler raised");
                    });
                }
            },
            route,
        ))
    }
}

fn parse_status(status: u16) -> StatusCode {
    StatusCode::from_u16(status).unwrap_or_else(|_| {
        tracing::error!(status, "the handler produced an invalid status code");
        StatusCode::INTERNAL_SERVER_ERROR
    })
}

async fn to_response(prepared: PreparedResponse) -> HttpResponse {
    match prepared.body {
        PreparedBody::Ready(body) => HttpResponse {
            status: parse_status(prepared.status),
            headers: prepared.headers,
            body,
            route: None,
        },
        PreparedBody::File(request) => {
            file_response(prepared.status, prepared.headers, request).await
        }
    }
}

/// Add `value` for `name` only when the handler did not set it itself.
fn fill_in(headers: &mut Headers, name: &str, value: &str) {
    if headers.contains(name) {
        return;
    }
    if let Err(error) = headers.insert(name, value) {
        tracing::warn!(%error, "could not add a header derived from the file");
    }
}

/// Open the file, work out what part of it was asked for, and describe the
/// result in the response.
async fn file_response(status: u16, mut headers: Headers, request: FileRequest) -> HttpResponse {
    let opened = match OpenFile::open(&request.path).await {
        Ok(opened) => opened,
        Err(error) if error.is_not_found() => {
            tracing::debug!(%error, "a handler asked for a file that is not there");
            return HttpResponse::text(StatusCode::NOT_FOUND, "Not Found");
        }
        Err(error) => {
            tracing::error!(%error, "a file could not be served");
            return HttpResponse::text(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error");
        }
    };

    let resolved = match request.range {
        Some((start, end)) => resolve_range(Some(start), end, opened.size),
        None => ResolvedRange::Full { size: opened.size },
    };

    fill_in(&mut headers, "content-type", &opened.content_type);
    fill_in(&mut headers, "accept-ranges", "bytes");
    if let Some(modified) = opened.last_modified() {
        fill_in(&mut headers, "last-modified", &modified);
    }
    if let Some(content_range) = resolved.content_range() {
        fill_in(&mut headers, "content-range", &content_range);
    }

    let status = match resolved {
        ResolvedRange::Full { .. } => parse_status(status),
        ResolvedRange::Partial { .. } => StatusCode::PARTIAL_CONTENT,
        ResolvedRange::Unsatisfiable { .. } => StatusCode::RANGE_NOT_SATISFIABLE,
    };

    match opened.into_body(resolved).await {
        Ok(body) => HttpResponse {
            status,
            headers,
            body: ResponseBody::File(body),
            route: None,
        },
        Err(error) => {
            tracing::error!(%error, "a file could not be positioned for sending");
            HttpResponse::text(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error")
        }
    }
}

fn internal_error(reason: &'static str) -> HttpResponse {
    tracing::error!(reason, "answering with 500");
    HttpResponse::text(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error")
}

impl PythonDispatch {
    /// Start the WebSocket handler coroutine.
    fn start_socket_handler(
        &self,
        request: WebSocketRequest,
    ) -> PyResult<impl Future<Output = ()> + Send + use<>> {
        let matched = self.routes.find(WEBSOCKET_METHOD, request.parts.path());
        let subprotocols = request.handshake.subprotocols().to_vec();

        let coroutine = Python::attach(|python| -> PyResult<_> {
            let scope = Py::new(
                python,
                WsScope::from_parts(python, &request.parts, &matched, &subprotocols)?,
            )?;
            let transport = Py::new(python, WsTransport::new(request.handshake))?;

            let application = self.application.bind(python);
            let entry_point = application.getattr(WEBSOCKET_ENTRY_POINT)?;
            let awaitable = entry_point.call1((scope, transport))?;
            pyo3_async_runtimes::into_future_with_locals(&self.locals, awaitable)
        })?;

        Ok(async move {
            if let Err(error) = coroutine.await {
                Python::attach(|python| {
                    tracing::error!(error = %error.value(python), "the WebSocket handler raised");
                });
            }
        })
    }
}

impl Dispatch for PythonDispatch {
    async fn handle_http(&self, request: HttpRequest) -> HttpResponse {
        let (mut receiver, handler, route) = match self.start_handler(request) {
            Ok(started) => started,
            Err(error) => {
                Python::attach(|python| {
                    tracing::error!(error = %error.value(python), "could not start the HTTP handler");
                });
                return internal_error("the handler could not be started");
            }
        };

        let mut handler = std::pin::pin!(tokio::spawn(handler));

        // Preferring the response branch means a handler that answers and then
        // returns in the same tick still has its answer used.
        let response = tokio::select! {
            biased;
            prepared = &mut receiver => match prepared {
                Ok(prepared) => to_response(prepared).await,
                Err(_) => internal_error("the handler ended without sending a response"),
            },
            outcome = &mut handler => {
                if let Err(error) = outcome
                    && !error.is_cancelled()
                {
                    tracing::error!(%error, "the HTTP handler task failed");
                }
                match receiver.try_recv() {
                    Ok(prepared) => to_response(prepared).await,
                    Err(_) => internal_error("the handler ended without sending a response"),
                }
            }
        };

        response.from_route(route)
    }

    async fn handle_webtransport(&self, request: WebTransportRequest) {
        let mut request = request;

        let has_handler = Python::attach(|python| {
            self.application
                .bind(python)
                .hasattr(WEBTRANSPORT_ENTRY_POINT)
                .unwrap_or(false)
        });
        if !has_handler {
            if let Err(error) = request.session.reject(StatusCode::NOT_IMPLEMENTED).await {
                tracing::debug!(%error, "could not refuse a WebTransport session");
            }
            return;
        }

        let matched = self.routes.find(WEBTRANSPORT_METHOD, request.parts.path());

        let started = Python::attach(|python| -> PyResult<_> {
            let scope = Py::new(
                python,
                WtScope::from_parts(python, &request.parts, &matched)?,
            )?;
            let session = Py::new(python, WtSession::new(request.session))?;

            let application = self.application.bind(python);
            let awaitable = application
                .getattr(WEBTRANSPORT_ENTRY_POINT)?
                .call1((scope, session))?;
            pyo3_async_runtimes::into_future_with_locals(&self.locals, awaitable)
        });

        match started {
            Ok(coroutine) => {
                if let Err(error) = coroutine.await {
                    Python::attach(|python| {
                        tracing::error!(
                            error = %error.value(python),
                            "the WebTransport handler raised"
                        );
                    });
                }
            }
            Err(error) => Python::attach(|python| {
                tracing::error!(
                    error = %error.value(python),
                    "could not start the WebTransport handler"
                );
            }),
        }
    }

    async fn handle_websocket(&self, request: WebSocketRequest) {
        let mut request = request;

        // An application without the entry point cannot answer, so the refusal
        // is made here rather than leaving the client waiting.
        let has_handler = Python::attach(|python| {
            self.application
                .bind(python)
                .hasattr(WEBSOCKET_ENTRY_POINT)
                .unwrap_or(false)
        });
        if !has_handler {
            reject(
                &mut request,
                StatusCode::NOT_IMPLEMENTED,
                "WebSocket is not supported here",
            );
            return;
        }

        match self.start_socket_handler(request) {
            Ok(handler) => handler.await,
            Err(error) => Python::attach(|python| {
                tracing::error!(
                    error = %error.value(python),
                    "could not start the WebSocket handler"
                );
            }),
        }
    }
}

fn reject(request: &mut WebSocketRequest, status: StatusCode, reason: &'static str) {
    if let Err(error) = request.handshake.reject(status, reason) {
        tracing::debug!(%error, "could not refuse a WebSocket upgrade");
    }
}
