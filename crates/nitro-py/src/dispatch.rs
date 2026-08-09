//! Handing a request to the Python application and collecting its answer.

use std::sync::Arc;

use http::StatusCode;
use nitro_core::router::RouteTable;
use nitro_core::transport::{Dispatch, HttpRequest, HttpResponse};
use pyo3::prelude::*;
use pyo3_async_runtimes::TaskLocals;
use tokio::sync::oneshot;

use crate::protocol::{HttpProtocol, PreparedResponse};
use crate::scope::HttpScope;

/// The name a Nitro application exposes to answer HTTP requests.
pub const HTTP_ENTRY_POINT: &str = "__handle_http__";

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
}

impl PythonDispatch {
    pub fn new(application: Py<PyAny>, routes: Arc<RouteTable>, locals: TaskLocals) -> Self {
        Self {
            application: Arc::new(application),
            routes,
            locals,
        }
    }

    /// Start the handler coroutine, returning a channel the response arrives on.
    fn start_handler(
        &self,
        request: HttpRequest,
    ) -> PyResult<(
        oneshot::Receiver<PreparedResponse>,
        impl Future<Output = ()> + Send + use<>,
    )> {
        let (responder, receiver) = oneshot::channel();

        // Matching happens before the handler is built so the scope can carry
        // the route and its captured parameters, and so an application never
        // has to work out which route a path belongs to.
        let matched = self
            .routes
            .find(request.parts.method.as_str(), request.parts.path());

        let coroutine = Python::attach(|python| -> PyResult<_> {
            let scope = Py::new(
                python,
                HttpScope::from_parts(python, &request.parts, &matched)?,
            )?;
            let protocol = Py::new(
                python,
                HttpProtocol::new(request.body, responder, request.disconnect),
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

        Ok((receiver, async move {
            if let Err(error) = coroutine.await {
                Python::attach(|python| {
                    tracing::error!(error = %error.value(python), "the HTTP handler raised");
                });
            }
        }))
    }
}

fn to_response(prepared: PreparedResponse) -> HttpResponse {
    let status = StatusCode::from_u16(prepared.status).unwrap_or_else(|_| {
        tracing::error!(
            status = prepared.status,
            "the handler produced an invalid status code"
        );
        StatusCode::INTERNAL_SERVER_ERROR
    });

    HttpResponse {
        status,
        headers: prepared.headers,
        body: prepared.body,
    }
}

fn internal_error(reason: &'static str) -> HttpResponse {
    tracing::error!(reason, "answering with 500");
    HttpResponse::text(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error")
}

impl Dispatch for PythonDispatch {
    async fn handle_http(&self, request: HttpRequest) -> HttpResponse {
        let (mut receiver, handler) = match self.start_handler(request) {
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
        tokio::select! {
            biased;
            prepared = &mut receiver => match prepared {
                Ok(prepared) => to_response(prepared),
                Err(_) => internal_error("the handler ended without sending a response"),
            },
            outcome = &mut handler => {
                if let Err(error) = outcome
                    && !error.is_cancelled()
                {
                    tracing::error!(%error, "the HTTP handler task failed");
                }
                match receiver.try_recv() {
                    Ok(prepared) => to_response(prepared),
                    Err(_) => internal_error("the handler ended without sending a response"),
                }
            }
        }
    }
}
