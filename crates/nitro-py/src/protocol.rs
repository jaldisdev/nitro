//! The object a handler answers a request through.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use bytes::Bytes;
use nitro_core::disconnect::DisconnectWatcher;
use nitro_core::headers::Headers as CoreHeaders;
use nitro_core::streaming::{self, StreamSender};
use nitro_core::transport::{RequestBody, ResponseBody};
use pyo3::exceptions::{PyRuntimeError, PyStopAsyncIteration, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use tokio::sync::oneshot;

/// A file the handler asked for, resolved once the response reaches the
/// transport. Opening it is asynchronous, and the methods that ask for it are
/// not, so what crosses is the request rather than the opened file.
#[derive(Debug)]
pub struct FileRequest {
    pub path: PathBuf,
    /// Inclusive byte range, when only part of the file was asked for.
    pub range: Option<(u64, Option<u64>)>,
}

/// What a handler asked the transport to send.
#[derive(Debug)]
pub enum PreparedBody {
    Ready(ResponseBody),
    File(FileRequest),
}

#[derive(Debug)]
pub struct PreparedResponse {
    pub status: u16,
    pub headers: CoreHeaders,
    pub body: PreparedBody,
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
    stream_capacity: usize,
}

impl HttpProtocol {
    pub fn new(
        body: RequestBody,
        responder: oneshot::Sender<PreparedResponse>,
        disconnect: DisconnectWatcher,
        stream_capacity: usize,
    ) -> Self {
        Self {
            body: Arc::new(tokio::sync::Mutex::new(BodyReader::Streaming(body))),
            responder: Arc::new(Mutex::new(Some(responder))),
            disconnect,
            stream_capacity,
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
            body: PreparedBody::Ready(ResponseBody::Empty),
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
            body: PreparedBody::Ready(ResponseBody::Bytes(Bytes::from(body))),
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
            body: PreparedBody::Ready(ResponseBody::Bytes(Bytes::from(body.into_bytes()))),
        })
    }

    /// Send a file. It is read as it goes out rather than loaded first, so the
    /// size of the file does not decide the size of the response in memory.
    ///
    /// `Content-Type` and `Last-Modified` are filled in from the file unless the
    /// handler supplied them.
    #[pyo3(signature = (status, headers=Vec::new(), path=String::new()))]
    fn response_file(
        &self,
        status: u16,
        headers: Vec<(String, String)>,
        path: String,
    ) -> PyResult<()> {
        self.respond(PreparedResponse {
            status,
            headers: build_headers(headers)?,
            body: PreparedBody::File(FileRequest {
                path: PathBuf::from(path),
                range: None,
            }),
        })
    }

    /// Send part of a file. `end` is inclusive and may be omitted to mean "to
    /// the last byte".
    ///
    /// The range is checked against the file's actual size once it is opened. A
    /// satisfiable range is answered with 206 and a `Content-Range`; one that
    /// starts past the end of the file is answered with 416, not with an empty
    /// success that would tell the client its range had been honoured.
    #[pyo3(signature = (status, headers=Vec::new(), path=String::new(), start=0, end=None))]
    fn response_file_range(
        &self,
        status: u16,
        headers: Vec<(String, String)>,
        path: String,
        start: u64,
        end: Option<u64>,
    ) -> PyResult<()> {
        self.respond(PreparedResponse {
            status,
            headers: build_headers(headers)?,
            body: PreparedBody::File(FileRequest {
                path: PathBuf::from(path),
                range: Some((start, end)),
            }),
        })
    }

    /// Start a streaming response and return the transport to write it through.
    ///
    /// The response goes out immediately; the body follows as chunks are sent.
    #[pyo3(signature = (status, headers=Vec::new()))]
    fn response_stream(
        &self,
        python: Python<'_>,
        status: u16,
        headers: Vec<(String, String)>,
    ) -> PyResult<Py<StreamTransport>> {
        let (sender, body) = streaming::channel(self.stream_capacity);
        self.respond(PreparedResponse {
            status,
            headers: build_headers(headers)?,
            body: PreparedBody::Ready(ResponseBody::Stream(body)),
        })?;

        Py::new(
            python,
            StreamTransport {
                sender: Mutex::new(Some(sender)),
            },
        )
    }
}

/// The writing end of a streaming response.
///
/// Sending waits when the transport is behind, which is the point: a producer
/// faster than its client is slowed to the client's pace instead of filling
/// memory with queued chunks.
#[pyclass(name = "StreamTransport", module = "nitro._nitro")]
pub struct StreamTransport {
    sender: Mutex<Option<StreamSender>>,
}

impl StreamTransport {
    fn active(&self) -> PyResult<StreamSender> {
        let guard = match self.sender.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        guard
            .as_ref()
            .cloned()
            .ok_or_else(|| PyRuntimeError::new_err("this response stream is closed"))
    }

    fn send<'py>(&self, python: Python<'py>, chunk: Bytes) -> PyResult<Bound<'py, PyAny>> {
        let sender = self.active()?;
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            sender
                .send(chunk)
                .await
                .map_err(|error| PyRuntimeError::new_err(error.to_string()))
        })
    }
}

#[pymethods]
impl StreamTransport {
    /// `await transport.send_bytes(chunk)`.
    fn send_bytes<'py>(&self, python: Python<'py>, chunk: Vec<u8>) -> PyResult<Bound<'py, PyAny>> {
        self.send(python, Bytes::from(chunk))
    }

    /// `await transport.send_str(text)`.
    fn send_str<'py>(&self, python: Python<'py>, text: String) -> PyResult<Bound<'py, PyAny>> {
        self.send(python, Bytes::from(text.into_bytes()))
    }

    /// End the response. Sending afterwards is an error.
    fn close(&self) {
        let mut guard = match self.sender.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        guard.take();
    }

    /// Whether the client has stopped reading.
    #[getter]
    fn closed(&self) -> bool {
        match self.active() {
            Ok(sender) => sender.is_closed(),
            Err(_) => true,
        }
    }

    /// How many more chunks fit before sending starts waiting.
    #[getter]
    fn capacity(&self) -> PyResult<usize> {
        Ok(self.active()?.capacity())
    }
}
