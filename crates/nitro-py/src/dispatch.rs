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
use pyo3::types::PyDict;
use pyo3_async_runtimes::TaskLocals;
use tokio::sync::oneshot;

use crate::protocol::{
    FileRequest, HandlerOutcome, HttpProtocol, PreparedBody, PreparedResponse, WsTransport,
    WtSession,
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
    /// Everything an HTTP request needs from Python, looked up once: the shim
    /// that runs a handler, the loop's two scheduling methods, and the context
    /// tasks are started in. Resolving these per request is several attribute
    /// lookups on the hot path for values that never change.
    http: Arc<HttpEntry>,
}

struct HttpEntry {
    serve: Py<PyAny>,
    call_soon_threadsafe: Py<PyAny>,
    create_task: Py<PyAny>,
    context: Py<PyAny>,
}

impl PythonDispatch {
    pub fn new(
        python: Python<'_>,
        application: Py<PyAny>,
        routes: Arc<RouteTable>,
        locals: TaskLocals,
        stream_capacity: usize,
    ) -> PyResult<Self> {
        let event_loop = locals.event_loop(python);
        let http = HttpEntry {
            serve: python.import("nitro.app")?.getattr("serve_http")?.unbind(),
            call_soon_threadsafe: event_loop.getattr("call_soon_threadsafe")?.unbind(),
            create_task: event_loop.getattr("create_task")?.unbind(),
            context: locals.context(python).clone().unbind(),
        };
        Ok(Self {
            application: Arc::new(application),
            routes,
            locals,
            stream_capacity,
            http: Arc::new(http),
        })
    }

    /// Hand the request to Python, returning the channel its outcome arrives on.
    ///
    /// The handler runs as one ordinary task on the event loop, started with the
    /// loop's own `call_soon_threadsafe`, and everything the transport needs to
    /// hear comes back over one channel: the response, or the fact that the
    /// handler ended without sending one. Nothing here waits for the task
    /// itself, which is what lets a handler send its response and keep working —
    /// a streaming body outlives the response it belongs to.
    fn start_handler(
        &self,
        request: HttpRequest,
    ) -> PyResult<(oneshot::Receiver<HandlerOutcome>, Option<String>)> {
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

        Python::attach(|python| -> PyResult<_> {
            let scope = Py::new(python, HttpScope::from_parts(python, parts, &matched)?)?;
            let protocol = Py::new(
                python,
                HttpProtocol::new(body, responder, disconnect, self.stream_capacity),
            )?;

            let coroutine = self.http.serve.bind(python).call1((
                self.application.bind(python),
                scope,
                protocol,
            ))?;

            // The context is passed rather than left to be copied here: this
            // runs on a runtime thread, whose context is not the one the worker
            // started in, and a task started from the wrong one would not see
            // the context variables an application set at startup.
            let arguments = (self.http.create_task.bind(python), coroutine);
            let keywords = PyDict::new(python);
            keywords.set_item("context", self.http.context.bind(python))?;
            self.http
                .call_soon_threadsafe
                .bind(python)
                .call(arguments, Some(&keywords))?;
            Ok(())
        })?;

        Ok((receiver, route))
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
        let (receiver, route) = match self.start_handler(request) {
            Ok(started) => started,
            Err(error) => {
                Python::attach(|python| {
                    tracing::error!(error = %error.value(python), "could not start the HTTP handler");
                });
                return internal_error("the handler could not be started");
            }
        };

        // One await on one channel. Whichever way serving ended — a response, a
        // return without one, a raise — the shim on the Python side says so, and
        // a dropped sender covers the case where the task never ran at all.
        let response = match receiver.await {
            Ok(HandlerOutcome::Response(prepared)) => to_response(prepared).await,
            Ok(HandlerOutcome::Ended) => {
                internal_error("the handler ended without sending a response")
            }
            Ok(HandlerOutcome::Failed(error)) => {
                tracing::error!(%error, "the HTTP handler raised");
                internal_error("the handler failed")
            }
            Err(_) => internal_error("the handler never ran"),
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
