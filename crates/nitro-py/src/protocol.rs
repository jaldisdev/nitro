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

//! The object a handler answers a request through.

use std::future::Future;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll, Waker};

use bytes::Bytes;
use http::StatusCode;
use nitro_core::disconnect::DisconnectWatcher;
use nitro_core::headers::Headers as CoreHeaders;
use nitro_core::streaming::{self, StreamSender};
use nitro_core::transport::{BodyError, RequestBody, ResponseBody};
use nitro_core::websocket::{
    WebSocketError, WebSocketHandshake, WebSocketMessage, WebSocketReceiver, WebSocketSender,
};
use nitro_core::webtransport::{
    IncomingStream, ReceiveHalf, SendHalf, SessionHandle, WebTransportError, WebTransportSession,
};
use pyo3::exceptions::{PyRuntimeError, PyStopAsyncIteration, PyStopIteration, PyValueError};
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

/// How serving a request ended, which is either a response or an explanation of
/// why there is not one.
///
/// One channel rather than two: the transport is waiting for exactly one of
/// these, and a handler that returns without answering has to be told apart from
/// one that is still running, or the request would wait for a response nobody is
/// going to send.
#[derive(Debug)]
pub enum HandlerOutcome {
    Response(PreparedResponse),
    /// The handler returned without sending anything.
    Ended,
    /// The handler raised, and the application did not turn it into a response.
    Failed(String),
}

/// Request body state. Reading it is destructive, so a body that has been
/// consumed reads as empty rather than raising — a handler that reads twice is
/// usually doing so defensively.
enum BodyReader {
    Streaming(RequestBody),
    Finished,
}

impl BodyReader {
    async fn read_all(&mut self) -> Result<Bytes, BodyError> {
        match std::mem::replace(self, Self::Finished) {
            Self::Streaming(body) => body.collect().await,
            Self::Finished => Ok(Bytes::new()),
        }
    }

    async fn next_chunk(&mut self) -> Result<Option<Bytes>, BodyError> {
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

/// An awaitable that already has its answer.
///
/// `await` on one of these never reaches the event loop: the iterator behind it
/// stops on its first step, which is how a coroutine returns a value without
/// suspending. That is the whole point of it — a value that is already in memory
/// should not cost a trip through the loop to hand over.
#[pyclass(name = "Ready", module = "nitro._nitro")]
struct Ready {
    value: Option<Py<PyAny>>,
}

#[pymethods]
impl Ready {
    fn __await__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __next__(&mut self) -> PyResult<()> {
        // `StopIteration(value)` is how an iterator hands a value to `await`.
        Err(match self.value.take() {
            Some(value) => PyStopIteration::new_err(value),
            None => PyStopIteration::new_err(()),
        })
    }
}

/// Hand `future` to Python as an awaitable, without involving the event loop if
/// it turns out to have finished already.
///
/// Polling once costs a poll. Not polling costs a scheduled callback on the loop
/// thread, the wake-up that delivers it and the hop back — which for a body that
/// arrived with the request, and is sitting in memory when the handler asks for
/// it, is the entire cost of reading it.
fn ready_or_scheduled<'py, F>(python: Python<'py>, future: F) -> PyResult<Bound<'py, PyAny>>
where
    F: Future<Output = PyResult<Py<PyAny>>> + Send + 'static,
{
    let mut future = Box::pin(future);

    // Entered so that a future which registers with the reactor or the timer
    // finds them, exactly as it would when polled by the runtime itself.
    let runtime = pyo3_async_runtimes::tokio::get_runtime();
    let polled = {
        let _guard = runtime.enter();
        future
            .as_mut()
            .poll(&mut Context::from_waker(Waker::noop()))
    };

    match polled {
        // The waker was never given anything to wake, and the future is done, so
        // there is nothing left for the loop to do.
        Poll::Ready(result) => Ok(Ready {
            value: Some(result?),
        }
        .into_pyobject(python)?
        .into_any()),
        // Polled once with a waker that does nothing, which a future must
        // tolerate: the runtime polls it again below with a real one, and the
        // most recent waker is the one that counts.
        Poll::Pending => pyo3_async_runtimes::tokio::future_into_py(python, future),
    }
}

#[pyclass(name = "HttpProtocol", module = "nitro._nitro")]
pub struct HttpProtocol {
    body: Arc<tokio::sync::Mutex<BodyReader>>,
    responder: Arc<Mutex<Option<oneshot::Sender<HandlerOutcome>>>>,
    disconnect: DisconnectWatcher,
    stream_capacity: usize,
}

impl HttpProtocol {
    pub fn new(
        body: RequestBody,
        responder: oneshot::Sender<HandlerOutcome>,
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
        self.finish(HandlerOutcome::Response(response), true)
    }

    /// Report how the handler ended, for the cases where nothing was sent.
    fn finish(&self, outcome: HandlerOutcome, complain: bool) -> PyResult<()> {
        let sender = match self.responder.lock() {
            Ok(mut responder) => responder.take(),
            Err(poisoned) => poisoned.into_inner().take(),
        };

        let Some(sender) = sender else {
            // Ending after answering is the ordinary case: the handler returns
            // once its response is on the way. Only a second *response* is a bug.
            if complain {
                return Err(PyRuntimeError::new_err(
                    "a response has already been sent for this request",
                ));
            }
            return Ok(());
        };
        if sender.send(outcome).is_err() {
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

fn body_error(error: BodyError) -> PyErr {
    PyRuntimeError::new_err(error.to_string())
}

#[pymethods]
impl HttpProtocol {
    /// Called by the shim that runs a handler, once it has returned. Ignored
    /// when a response was already sent, which is the ordinary case.
    fn _handler_ended(&self) -> PyResult<()> {
        self.finish(HandlerOutcome::Ended, false)
    }

    /// Called by the shim that runs a handler when it raised instead of
    /// answering. The transport turns this into a 500; the application has
    /// already had its chance to handle the exception itself.
    fn _handler_failed(&self, error: String) -> PyResult<()> {
        self.finish(HandlerOutcome::Failed(error), false)
    }

    /// `await protocol()` — the rest of the request body as one value.
    fn __call__<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let body = Arc::clone(&self.body);
        ready_or_scheduled(python, async move {
            let chunk = body.lock().await.read_all().await.map_err(body_error)?;
            Python::attach(|python| Ok(PyBytes::new(python, &chunk).into_any().unbind()))
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

// ── WebSocket ────────────────────────────────────────────────────────────────

/// The stage a socket has reached, kept outside the locks so it can be read
/// without waiting on whatever operation is in flight.
const PHASE_PENDING: u8 = 0;
const PHASE_OPEN: u8 = 1;
const PHASE_CLOSED: u8 = 2;

/// A WebSocket connection, from the handshake through to close.
///
/// The reading and writing halves are held separately. A handler that relays
/// messages in one task while waiting for them in another — which is what most
/// of them do — would otherwise have the two waiting on each other.
#[pyclass(name = "WsTransport", module = "nitro._nitro")]
pub struct WsTransport {
    handshake: Arc<tokio::sync::Mutex<Option<WebSocketHandshake>>>,
    sender: Arc<tokio::sync::Mutex<Option<WebSocketSender>>>,
    receiver: Arc<tokio::sync::Mutex<Option<WebSocketReceiver>>>,
    phase: Arc<AtomicU8>,
    subprotocols: Vec<String>,
}

impl WsTransport {
    pub fn new(handshake: WebSocketHandshake) -> Self {
        Self {
            subprotocols: handshake.subprotocols().to_vec(),
            handshake: Arc::new(tokio::sync::Mutex::new(Some(handshake))),
            sender: Arc::new(tokio::sync::Mutex::new(None)),
            receiver: Arc::new(tokio::sync::Mutex::new(None)),
            phase: Arc::new(AtomicU8::new(PHASE_PENDING)),
        }
    }
}

fn socket_error(error: WebSocketError) -> PyErr {
    PyRuntimeError::new_err(error.to_string())
}

fn already_answered() -> PyErr {
    PyRuntimeError::new_err("this handshake has already been answered")
}

/// Read one message, translating it for Python. `None` means the connection
/// has ended.
async fn next_message(
    receiver: &Arc<tokio::sync::Mutex<Option<WebSocketReceiver>>>,
    phase: &Arc<AtomicU8>,
) -> PyResult<Option<Py<PyAny>>> {
    let mut guard = receiver.lock().await;
    let Some(active) = guard.as_mut() else {
        return Ok(None);
    };

    loop {
        let Some(message) = active.receive().await else {
            *guard = None;
            phase.store(PHASE_CLOSED, Ordering::Release);
            return Ok(None);
        };

        match message.map_err(socket_error)? {
            WebSocketMessage::Text(text) => {
                return Python::attach(|python| {
                    Ok(Some(text.into_pyobject(python)?.into_any().unbind()))
                });
            }
            WebSocketMessage::Binary(data) => {
                return Python::attach(|python| {
                    Ok(Some(PyBytes::new(python, &data).into_any().unbind()))
                });
            }
            WebSocketMessage::Close { .. } => {
                *guard = None;
                phase.store(PHASE_CLOSED, Ordering::Release);
                return Ok(None);
            }
            // Answered by the framing layer; a handler has no use for them.
            WebSocketMessage::Ping(_) | WebSocketMessage::Pong(_) => continue,
        }
    }
}

#[pymethods]
impl WsTransport {
    /// The subprotocols the client offered, in the order it prefers them.
    #[getter]
    fn subprotocols(&self) -> Vec<String> {
        self.subprotocols.clone()
    }

    /// `await transport.accept()` — complete the handshake.
    ///
    /// A subprotocol may only be chosen from those the client offered.
    #[pyo3(signature = (subprotocol=None))]
    fn accept<'py>(
        &self,
        python: Python<'py>,
        subprotocol: Option<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let handshake = Arc::clone(&self.handshake);
        let sender = Arc::clone(&self.sender);
        let receiver = Arc::clone(&self.receiver);
        let phase = Arc::clone(&self.phase);

        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let mut pending = handshake.lock().await;
            let Some(open) = pending.as_mut() else {
                return Err(already_answered());
            };

            let connection = open.accept(subprotocol).await.map_err(socket_error)?;
            *pending = None;

            let (writing, reading) = connection.split();
            *sender.lock().await = Some(writing);
            *receiver.lock().await = Some(reading);
            phase.store(PHASE_OPEN, Ordering::Release);
            Ok(())
        })
    }

    /// `await transport.reject(...)` — refuse the upgrade and answer with an
    /// ordinary HTTP response instead.
    #[pyo3(signature = (status=403, reason=String::new()))]
    fn reject<'py>(
        &self,
        python: Python<'py>,
        status: u16,
        reason: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let status = StatusCode::from_u16(status)
            .map_err(|_| PyValueError::new_err(format!("{status} is not an HTTP status code")))?;
        let handshake = Arc::clone(&self.handshake);
        let phase = Arc::clone(&self.phase);

        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let mut pending = handshake.lock().await;
            let Some(open) = pending.as_mut() else {
                return Err(already_answered());
            };

            open.reject(status, Bytes::from(reason.into_bytes()))
                .map_err(socket_error)?;
            *pending = None;
            phase.store(PHASE_CLOSED, Ordering::Release);
            Ok(())
        })
    }

    /// `await transport.receive()` — the next message.
    ///
    /// Text arrives as `str` and binary as `bytes`. `None` means the connection
    /// has ended.
    fn receive<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let receiver = Arc::clone(&self.receiver);
        let phase = Arc::clone(&self.phase);

        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            match next_message(&receiver, &phase).await? {
                Some(message) => Ok(message),
                None => Python::attach(|python| Ok(python.None())),
            }
        })
    }

    fn __aiter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __anext__<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let receiver = Arc::clone(&self.receiver);
        let phase = Arc::clone(&self.phase);

        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            match next_message(&receiver, &phase).await? {
                Some(message) => Ok(message),
                None => Err(PyStopAsyncIteration::new_err(())),
            }
        })
    }

    fn send_str<'py>(&self, python: Python<'py>, text: String) -> PyResult<Bound<'py, PyAny>> {
        self.send_message(python, WebSocketMessage::Text(text))
    }

    fn send_bytes<'py>(&self, python: Python<'py>, data: Vec<u8>) -> PyResult<Bound<'py, PyAny>> {
        self.send_message(python, WebSocketMessage::Binary(Bytes::from(data)))
    }

    /// `await transport.close()` — end the connection.
    #[pyo3(signature = (code=1000, reason=String::new()))]
    fn close<'py>(
        &self,
        python: Python<'py>,
        code: u16,
        reason: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let sender = Arc::clone(&self.sender);
        let receiver = Arc::clone(&self.receiver);
        let phase = Arc::clone(&self.phase);

        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            if let Some(writing) = sender.lock().await.as_mut() {
                // A peer that has already gone is not worth reporting: the
                // connection ends up closed either way, which is what was asked
                // for.
                if let Err(error) = writing.close(Some(code), reason).await {
                    tracing::debug!(%error, "closing an already-closed WebSocket");
                }
            }
            *sender.lock().await = None;
            *receiver.lock().await = None;
            phase.store(PHASE_CLOSED, Ordering::Release);
            Ok(())
        })
    }

    /// Whether the handshake has been accepted and the connection is still
    /// open. Reading this never waits on an operation in flight.
    #[getter]
    fn connected(&self) -> bool {
        self.phase.load(Ordering::Acquire) == PHASE_OPEN
    }
}

impl WsTransport {
    fn send_message<'py>(
        &self,
        python: Python<'py>,
        message: WebSocketMessage,
    ) -> PyResult<Bound<'py, PyAny>> {
        let sender = Arc::clone(&self.sender);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let mut guard = sender.lock().await;
            let Some(writing) = guard.as_mut() else {
                return Err(PyRuntimeError::new_err(
                    "this WebSocket is not open; accept the handshake first",
                ));
            };
            writing.send(message).await.map_err(socket_error)
        })
    }
}

// ── WebTransport ─────────────────────────────────────────────────────────────

/// A WebTransport session, from the `CONNECT` through to close.
///
/// Once accepted, datagrams and streams are reached through a handle that
/// carries its own synchronisation, so a handler can work on several at once
/// without one waiting on another.
#[pyclass(name = "WtSession", module = "nitro._nitro")]
pub struct WtSession {
    pending: Arc<tokio::sync::Mutex<WebTransportSession>>,
    handle: Arc<Mutex<Option<SessionHandle>>>,
    phase: Arc<AtomicU8>,
}

impl WtSession {
    pub fn new(session: WebTransportSession) -> Self {
        Self {
            pending: Arc::new(tokio::sync::Mutex::new(session)),
            handle: Arc::new(Mutex::new(None)),
            phase: Arc::new(AtomicU8::new(PHASE_PENDING)),
        }
    }

    fn opened(&self) -> PyResult<SessionHandle> {
        let guard = match self.handle.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        guard
            .clone()
            .ok_or_else(|| PyRuntimeError::new_err("this WebTransport session is not open"))
    }
}

fn session_error(error: WebTransportError) -> PyErr {
    PyRuntimeError::new_err(error.to_string())
}

#[pymethods]
impl WtSession {
    /// `await session.accept()` — establish the session.
    fn accept<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let pending = Arc::clone(&self.pending);
        let opened = Arc::clone(&self.handle);
        let phase = Arc::clone(&self.phase);

        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let mut session = pending.lock().await;
            session.accept().await.map_err(session_error)?;
            let handle = session.handle().map_err(session_error)?;

            match opened.lock() {
                Ok(mut slot) => *slot = Some(handle),
                Err(poisoned) => *poisoned.into_inner() = Some(handle),
            }
            phase.store(PHASE_OPEN, Ordering::Release);
            Ok(())
        })
    }

    /// `await session.reject(...)` — refuse it with an ordinary HTTP status.
    #[pyo3(signature = (status=403))]
    fn reject<'py>(&self, python: Python<'py>, status: u16) -> PyResult<Bound<'py, PyAny>> {
        let status = StatusCode::from_u16(status)
            .map_err(|_| PyValueError::new_err(format!("{status} is not an HTTP status code")))?;
        let pending = Arc::clone(&self.pending);
        let phase = Arc::clone(&self.phase);

        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            pending
                .lock()
                .await
                .reject(status)
                .await
                .map_err(session_error)?;
            phase.store(PHASE_CLOSED, Ordering::Release);
            Ok(())
        })
    }

    #[getter]
    fn connected(&self) -> bool {
        self.phase.load(Ordering::Acquire) == PHASE_OPEN
    }

    /// Send a datagram. Delivery is not guaranteed and order is not preserved.
    fn send_datagram(&self, payload: Vec<u8>) -> PyResult<()> {
        self.opened()?
            .send_datagram(Bytes::from(payload))
            .map_err(session_error)
    }

    /// `await session.receive_datagram()` — the next datagram, or `None` once
    /// the session has ended.
    fn receive_datagram<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let handle = self.opened()?;
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            match handle.receive_datagram().await.map_err(session_error)? {
                Some(payload) => {
                    Python::attach(|python| Ok(PyBytes::new(python, &payload).into_any().unbind()))
                }
                None => Python::attach(|python| Ok(python.None())),
            }
        })
    }

    /// `await session.accept_stream()` — a bidirectional stream the client
    /// opened, or `None` once the session has ended.
    fn accept_stream<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let handle = self.opened()?;
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            match handle.accept_stream().await.map_err(session_error)? {
                Some(IncomingStream::Bidirectional { send, receive }) => Python::attach(|python| {
                    Ok(Py::new(python, WtStream::duplex(*send, *receive))?.into_any())
                }),
                Some(IncomingStream::Unidirectional(receive)) => Python::attach(|python| {
                    Ok(Py::new(python, WtStream::receive_only(*receive))?.into_any())
                }),
                None => Python::attach(|python| Ok(python.None())),
            }
        })
    }

    /// `await session.accept_incoming()` — a unidirectional stream the client
    /// opened, which can only be read.
    fn accept_incoming<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let handle = self.opened()?;
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            match handle.accept_incoming().await.map_err(session_error)? {
                Some(receive) => Python::attach(|python| {
                    Ok(Py::new(python, WtStream::receive_only(receive))?.into_any())
                }),
                None => Python::attach(|python| Ok(python.None())),
            }
        })
    }

    /// `await session.open_stream()` — a bidirectional stream to the client.
    fn open_stream<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let handle = self.opened()?;
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let (send, receive) = handle.open_stream().await.map_err(session_error)?;
            Python::attach(
                |python| Ok(Py::new(python, WtStream::duplex(send, receive))?.into_any()),
            )
        })
    }

    /// `await session.open_outgoing()` — a unidirectional stream to the client,
    /// which can only be written.
    fn open_outgoing<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let handle = self.opened()?;
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let send = handle.open_outgoing().await.map_err(session_error)?;
            Python::attach(|python| Ok(Py::new(python, WtStream::send_only(send))?.into_any()))
        })
    }

    /// End the session.
    fn close<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let pending = Arc::clone(&self.pending);
        let opened = Arc::clone(&self.handle);
        let phase = Arc::clone(&self.phase);

        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            pending.lock().await.close();
            match opened.lock() {
                Ok(mut slot) => *slot = None,
                Err(poisoned) => *poisoned.into_inner() = None,
            }
            phase.store(PHASE_CLOSED, Ordering::Release);
            Ok(())
        })
    }
}

/// One WebTransport stream. A unidirectional stream carries only the half its
/// direction allows, and using the other half is an error rather than a silent
/// no-op.
#[pyclass(name = "WtStream", module = "nitro._nitro")]
pub struct WtStream {
    send: Arc<tokio::sync::Mutex<Option<SendHalf>>>,
    receive: Arc<tokio::sync::Mutex<Option<ReceiveHalf>>>,
}

impl WtStream {
    fn duplex(send: SendHalf, receive: ReceiveHalf) -> Self {
        Self {
            send: Arc::new(tokio::sync::Mutex::new(Some(send))),
            receive: Arc::new(tokio::sync::Mutex::new(Some(receive))),
        }
    }

    fn send_only(send: SendHalf) -> Self {
        Self {
            send: Arc::new(tokio::sync::Mutex::new(Some(send))),
            receive: Arc::new(tokio::sync::Mutex::new(None)),
        }
    }

    fn receive_only(receive: ReceiveHalf) -> Self {
        Self {
            send: Arc::new(tokio::sync::Mutex::new(None)),
            receive: Arc::new(tokio::sync::Mutex::new(Some(receive))),
        }
    }
}

#[pymethods]
impl WtStream {
    /// `await stream.write(data)`.
    fn write<'py>(&self, python: Python<'py>, data: Vec<u8>) -> PyResult<Bound<'py, PyAny>> {
        let half = Arc::clone(&self.send);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let mut guard = half.lock().await;
            let Some(send) = guard.as_mut() else {
                return Err(PyRuntimeError::new_err("this stream cannot be written to"));
            };
            send.write(&data).await.map_err(session_error)
        })
    }

    /// `await stream.read(limit)` — up to `limit` bytes. An empty result means
    /// the stream has ended.
    #[pyo3(signature = (limit=65536))]
    fn read<'py>(&self, python: Python<'py>, limit: usize) -> PyResult<Bound<'py, PyAny>> {
        let half = Arc::clone(&self.receive);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let mut guard = half.lock().await;
            let Some(receive) = guard.as_mut() else {
                return Err(PyRuntimeError::new_err("this stream cannot be read from"));
            };
            let data = receive.read(limit).await.map_err(session_error)?;
            Python::attach(|python| Ok(PyBytes::new(python, &data).unbind()))
        })
    }

    /// `await stream.read_all()` — everything up to the end of the stream.
    fn read_all<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let half = Arc::clone(&self.receive);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let mut guard = half.lock().await;
            let Some(receive) = guard.as_mut() else {
                return Err(PyRuntimeError::new_err("this stream cannot be read from"));
            };
            let data = receive.read_to_end().await.map_err(session_error)?;
            Python::attach(|python| Ok(PyBytes::new(python, &data).unbind()))
        })
    }

    /// `await stream.finish()` — no more will be written.
    fn finish<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let half = Arc::clone(&self.send);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let mut guard = half.lock().await;
            match guard.as_mut() {
                Some(send) => send.finish().await.map_err(session_error),
                None => Ok(()),
            }
        })
    }

    #[getter]
    fn writable(&self) -> bool {
        self.send.try_lock().is_ok_and(|half| half.is_some())
    }

    #[getter]
    fn readable(&self) -> bool {
        self.receive.try_lock().is_ok_and(|half| half.is_some())
    }
}
