//! The object a handler answers a request through.

use std::sync::{Arc, Mutex};

use bytes::Bytes;
use nitro_core::disconnect::DisconnectWatcher;
use nitro_core::headers::Headers as CoreHeaders;
use nitro_core::transport::{RequestBody, ResponseBody};
use pyo3::exceptions::{PyRuntimeError, PyStopAsyncIteration, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use tokio::sync::oneshot;

/// What a handler asked the transport to send.
#[derive(Debug)]
pub struct PreparedResponse {
    pub status: u16,
    pub headers: CoreHeaders,
    pub body: ResponseBody,
}

/// Request body state. Reading it is destructive, so a body that has been
/// consumed reads as empty rather than raising — a handler that reads twice is
/// usually doing so defensively.
enum BodyReader {
    Streaming(RequestBody),
    Finished,
}

impl BodyReader {
    async fn read_all(&mut self) -> Result<Bytes, hyper::Error> {
        match std::mem::replace(self, Self::Finished) {
            Self::Streaming(body) => body.collect().await,
            Self::Finished => Ok(Bytes::new()),
        }
    }

    async fn next_chunk(&mut self) -> Result<Option<Bytes>, hyper::Error> {
        let Self::Streaming(body) = self else {
            return Ok(None);
        };
        match body.next_chunk().await {
            Some(Ok(chunk)) => Ok(Some(chunk)),
            Some(Err(error)) => Err(error),
            None => {
                *self = Self::Finished;
                Ok(None)
            }
        }
    }
}

#[pyclass(name = "HttpProtocol", module = "nitro._nitro")]
pub struct HttpProtocol {
    body: Arc<tokio::sync::Mutex<BodyReader>>,
    responder: Arc<Mutex<Option<oneshot::Sender<PreparedResponse>>>>,
    disconnect: DisconnectWatcher,
}

impl HttpProtocol {
    pub fn new(
        body: RequestBody,
        responder: oneshot::Sender<PreparedResponse>,
        disconnect: DisconnectWatcher,
    ) -> Self {
        Self {
            body: Arc::new(tokio::sync::Mutex::new(BodyReader::Streaming(body))),
            responder: Arc::new(Mutex::new(Some(responder))),
            disconnect,
        }
    }

    /// Hand a response to the transport. Only the first one is accepted; a
    /// second is a handler bug and is reported rather than silently ignored.
    fn respond(&self, response: PreparedResponse) -> PyResult<()> {
        let sender = match self.responder.lock() {
            Ok(mut responder) => responder.take(),
            Err(poisoned) => poisoned.into_inner().take(),
        };

        let Some(sender) = sender else {
            return Err(PyRuntimeError::new_err(
                "a response has already been sent for this request",
            ));
        };
        if sender.send(response).is_err() {
            tracing::debug!("response discarded because the connection had already closed");
        }
        Ok(())
    }
}

fn build_headers(pairs: Vec<(String, String)>) -> PyResult<CoreHeaders> {
    let mut headers = CoreHeaders::new();
    for (name, value) in pairs {
        headers
            .append(&name, &value)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
    }
    Ok(headers)
}

fn body_error(error: hyper::Error) -> PyErr {
    PyRuntimeError::new_err(format!("reading the request body failed: {error}"))
}

#[pymethods]
impl HttpProtocol {
    /// `await protocol()` — the rest of the request body as one value.
    fn __call__<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let body = Arc::clone(&self.body);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let chunk = body.lock().await.read_all().await.map_err(body_error)?;
            Python::attach(|python| Ok(PyBytes::new(python, &chunk).unbind()))
        })
    }

    /// `async for chunk in protocol` — the request body as it arrives.
    fn __aiter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __anext__<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let body = Arc::clone(&self.body);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            match body.lock().await.next_chunk().await.map_err(body_error)? {
                Some(chunk) => Python::attach(|python| Ok(PyBytes::new(python, &chunk).unbind())),
                None => Err(PyStopAsyncIteration::new_err(())),
            }
        })
    }

    /// `await protocol.client_disconnect()` — resolves when there is no longer
    /// anybody to send to, either because the connection ended or because the
    /// server started shutting down.
    ///
    /// Nothing is cancelled on the handler's behalf; reacting is up to the
    /// handler.
    fn client_disconnect<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let disconnect = self.disconnect.clone();
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            disconnect.wait().await;
            Ok(())
        })
    }

    /// Whether the client has already gone away.
    #[getter]
    fn disconnected(&self) -> bool {
        self.disconnect.is_disconnected()
    }

    #[pyo3(signature = (status, headers=Vec::new()))]
    fn response_empty(&self, status: u16, headers: Vec<(String, String)>) -> PyResult<()> {
        self.respond(PreparedResponse {
            status,
            headers: build_headers(headers)?,
            body: ResponseBody::Empty,
        })
    }

    #[pyo3(signature = (status, headers=Vec::new(), body=Vec::new()))]
    fn response_bytes(
        &self,
        status: u16,
        headers: Vec<(String, String)>,
        body: Vec<u8>,
    ) -> PyResult<()> {
        self.respond(PreparedResponse {
            status,
            headers: build_headers(headers)?,
            body: ResponseBody::Bytes(Bytes::from(body)),
        })
    }

    #[pyo3(signature = (status, headers=Vec::new(), body=String::new()))]
    fn response_str(
        &self,
        status: u16,
        headers: Vec<(String, String)>,
        body: String,
    ) -> PyResult<()> {
        self.respond(PreparedResponse {
            status,
            headers: build_headers(headers)?,
            body: ResponseBody::Bytes(Bytes::from(body.into_bytes())),
        })
    }
}
