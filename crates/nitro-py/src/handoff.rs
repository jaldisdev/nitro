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

//! Getting an arrived request from a runtime thread to the event loop.
//!
//! The obvious way is to schedule one callback per request, which is what this
//! server used to do. It costs an interpreter lock acquisition on the runtime
//! thread and a wake-up of the loop thread, every request, and both are
//! contended: measured against a server that does the same work, Nitro spent
//! four times the futex operations and six times the poller wake-ups per
//! request, while performing exactly the same reads and writes.
//!
//! So nothing crosses here as a Python object. A request is pushed onto a queue
//! and the loop is woken by writing one byte to a socket it is already watching
//! — no lock, and only when the loop is not already on its way. Everything
//! queued since the last visit is then built and started in one go, on the
//! loop's own thread. Under load that turns a wake-up per request into a
//! wake-up per batch, and the busier the server is the larger the batch.

use std::io::{Read, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use nitro_core::disconnect::DisconnectWatcher;
use nitro_core::router::RouteMatch;
use nitro_core::transport::{RequestBody, RequestParts};
use pyo3::prelude::*;
use tokio::sync::oneshot;

use crate::protocol::{HandlerOutcome, HttpProtocol};
use crate::scope::HttpScope;

/// A request that has arrived and has not been made into Python objects yet.
pub struct Pending {
    pub parts: RequestParts,
    pub body: RequestBody,
    pub disconnect: DisconnectWatcher,
    pub responder: oneshot::Sender<HandlerOutcome>,
    pub matched: RouteMatch,
}

/// The queue itself, and the socket that tells the loop to come and look.
pub struct Handoff {
    queued: Mutex<Vec<Pending>>,
    /// Whether the loop has already been told. Set by whoever writes the byte
    /// and cleared by the drain, so a busy stretch costs one wake-up rather
    /// than one per request.
    notified: AtomicBool,
    waker: Mutex<Waker>,
}

impl Handoff {
    pub fn new(waker: Waker) -> Self {
        Self {
            queued: Mutex::new(Vec::new()),
            notified: AtomicBool::new(false),
            waker: Mutex::new(waker),
        }
    }

    /// Hand a request over. Called from a runtime thread, without the
    /// interpreter lock, which is the whole point of it.
    pub fn push(&self, pending: Pending) {
        match self.queued.lock() {
            Ok(mut queued) => queued.push(pending),
            Err(poisoned) => poisoned.into_inner().push(pending),
        }
        if !self.notified.swap(true, Ordering::AcqRel) {
            let mut waker = match self.waker.lock() {
                Ok(waker) => waker,
                Err(poisoned) => poisoned.into_inner(),
            };
            if let Err(error) = waker.wake() {
                tracing::error!(%error, "could not wake the event loop");
            }
        }
    }

    /// Everything queued since the last visit.
    ///
    /// The flag is cleared before the queue is taken, not after: a request
    /// arriving in between then finds it clear, writes its byte, and is
    /// collected by the next drain. The other order would let that request sit
    /// in the queue with nobody coming for it.
    fn take(&self) -> Vec<Pending> {
        self.notified.store(false, Ordering::Release);
        match self.queued.lock() {
            Ok(mut queued) => std::mem::take(&mut *queued),
            Err(poisoned) => std::mem::take(&mut *poisoned.into_inner()),
        }
    }
}

/// The writing end of the socket pair the loop watches.
pub struct Waker {
    #[cfg(unix)]
    stream: std::os::unix::net::UnixStream,
}

impl Waker {
    /// # Safety
    ///
    /// `descriptor` must be the writing end of a socket pair that nothing else
    /// owns, and must stay open for as long as this does.
    #[cfg(unix)]
    pub unsafe fn from_descriptor(descriptor: i32) -> Self {
        use std::os::fd::FromRawFd;

        // SAFETY: the caller owns the descriptor and gives it up here.
        let stream = unsafe { std::os::unix::net::UnixStream::from_raw_fd(descriptor) };
        Self { stream }
    }

    #[cfg(unix)]
    fn wake(&mut self) -> std::io::Result<()> {
        match self.stream.write(&[1]) {
            // A full pipe means the loop has not read what is already there,
            // which is exactly the case where another byte would tell it
            // nothing new.
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => Ok(()),
            Err(error) => Err(error),
            Ok(_) => Ok(()),
        }
    }

    /// There is no socket pair to write to on a platform without one, and no
    /// `Waker` to call this on either: `install` answers `None` before a
    /// `Handoff` is built, so the caller schedules each request itself. This
    /// exists so `push` compiles everywhere, not to run.
    #[cfg(not(unix))]
    fn wake(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

/// What the event loop calls when the socket says there is something to collect.
#[pyclass(name = "RequestDrain", module = "nitro._nitro")]
pub struct RequestDrain {
    handoff: Arc<Handoff>,
    /// The reading end, drained on every visit so the loop stops reporting it.
    #[cfg(unix)]
    reader: Mutex<std::os::unix::net::UnixStream>,
    entry: Py<PyAny>,
    create_task: Py<PyAny>,
    stream_capacity: usize,
}

impl RequestDrain {
    #[cfg(unix)]
    pub fn new(
        handoff: Arc<Handoff>,
        reader: std::os::unix::net::UnixStream,
        entry: Py<PyAny>,
        create_task: Py<PyAny>,
        stream_capacity: usize,
    ) -> Self {
        Self {
            handoff,
            reader: Mutex::new(reader),
            entry,
            create_task,
            stream_capacity,
        }
    }
}

#[pymethods]
impl RequestDrain {
    /// Build and start everything that has arrived, in one visit.
    fn __call__(&self, python: Python<'_>) -> PyResult<()> {
        #[cfg(unix)]
        {
            let mut buffer = [0_u8; 256];
            let mut reader = match self.reader.lock() {
                Ok(reader) => reader,
                Err(poisoned) => poisoned.into_inner(),
            };
            // Non-blocking, so this ends in `WouldBlock` once it is empty.
            while let Ok(read) = reader.read(&mut buffer) {
                if read < buffer.len() {
                    break;
                }
            }
        }

        for pending in self.handoff.take() {
            if let Err(error) = self.start(python, pending) {
                tracing::error!(error = %error.value(python), "could not start a handler");
            }
        }
        Ok(())
    }
}

impl RequestDrain {
    fn start(&self, python: Python<'_>, pending: Pending) -> PyResult<()> {
        let scope = Py::new(
            python,
            HttpScope::from_parts(python, pending.parts, &pending.matched)?,
        )?;
        let protocol = Py::new(
            python,
            HttpProtocol::new(
                pending.body,
                pending.responder,
                pending.disconnect,
                self.stream_capacity,
            ),
        )?;

        let coroutine = self.entry.bind(python).call1((scope, protocol))?;
        self.create_task.bind(python).call1((coroutine,))?;
        Ok(())
    }
}

/// Make the socket pair the loop watches, and wire the drain onto it.
///
/// The pair is Python's own, so the loop can be told to watch it the way it
/// watches anything else, and so this works wherever `add_reader` does.
#[cfg(unix)]
pub fn install(
    python: Python<'_>,
    event_loop: &Bound<'_, PyAny>,
    entry: Py<PyAny>,
    stream_capacity: usize,
) -> PyResult<Option<Arc<Handoff>>> {
    use std::os::fd::FromRawFd;

    let pair = python.import("socket")?.call_method0("socketpair")?;
    let reader = pair.get_item(0)?;
    let writer = pair.get_item(1)?;
    reader.call_method1("setblocking", (false,))?;
    writer.call_method1("setblocking", (false,))?;

    // Detached rather than kept: the sockets close their descriptors when they
    // are collected, and these have to outlive them.
    let reading: i32 = reader.call_method0("detach")?.extract()?;
    let writing: i32 = writer.call_method0("detach")?.extract()?;

    // SAFETY: both descriptors were just detached from the sockets that owned
    // them, so nothing else holds or will close them.
    let waker = unsafe { Waker::from_descriptor(writing) };
    let stream = unsafe { std::os::unix::net::UnixStream::from_raw_fd(reading) };

    let handoff = Arc::new(Handoff::new(waker));
    let drain = RequestDrain::new(
        Arc::clone(&handoff),
        stream,
        entry,
        event_loop.getattr("create_task")?.unbind(),
        stream_capacity,
    );
    event_loop.call_method1("add_reader", (reading, Py::new(python, drain)?))?;
    Ok(Some(handoff))
}

/// Without a socket pair to write to from a thread holding no interpreter
/// lock, there is nothing to install; the caller schedules each request
/// instead.
#[cfg(not(unix))]
pub fn install(
    _python: Python<'_>,
    _event_loop: &Bound<'_, PyAny>,
    _entry: Py<PyAny>,
    _stream_capacity: usize,
) -> PyResult<Option<Arc<Handoff>>> {
    Ok(None)
}
