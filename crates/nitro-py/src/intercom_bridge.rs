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

//! Reaching Intercom from inside a Nitro worker.
//!
//! A Nitro project talks to the same publish/subscribe core as the standalone
//! package, but in this process and configured from the project's own settings
//! — it never imports or configures that package. A WebSocket or WebTransport
//! handler publishing a message therefore stays inside one process and one
//! interpreter.
//!
//! The Python-to-MessagePack conversion below is deliberately a copy of the one
//! in the standalone bindings rather than shared code. The two are separate
//! distributions that must not depend on one another, and what they encode is a
//! wire contract: if one changes, the other has to change with it, and a
//! deliberate copy makes that visible where a shared helper would hide it.

use std::sync::Arc;

use bytes::Bytes;
use intercom_core::channel::{ChannelConfig, unique_channel};
use intercom_core::codec::{self, Value};
use intercom_core::redis::{ChannelReader, Intercom as CoreIntercom, IntercomError, Subscription};
use pyo3::exceptions::{PyConnectionError, PyRuntimeError, PyStopAsyncIteration, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

fn intercom_error(error: IntercomError) -> PyErr {
    match error {
        IntercomError::Connection(reason) => PyConnectionError::new_err(reason),
        IntercomError::Command(reason) => PyRuntimeError::new_err(reason),
    }
}

// ── value conversion ─────────────────────────────────────────────────────────

/// Convert a Python value into a message.
///
/// Only types with an unambiguous MessagePack counterpart are accepted.
/// Anything else is refused rather than coerced, because guessing here would
/// mean the receiver gets something the sender did not mean to send.
fn to_value(object: &Bound<'_, PyAny>) -> PyResult<Value> {
    if object.is_none() {
        return Ok(Value::Nil);
    }
    // Checked before integers, since a bool is an int in Python and would
    // otherwise cross the wire as 0 or 1.
    if let Ok(boolean) = object.cast::<PyBool>() {
        return Ok(Value::Boolean(boolean.is_true()));
    }
    if let Ok(integer) = object.cast::<PyInt>() {
        return Ok(Value::Integer(integer.extract::<i64>()?.into()));
    }
    if let Ok(float) = object.cast::<PyFloat>() {
        return Ok(Value::F64(float.extract::<f64>()?));
    }
    if let Ok(text) = object.cast::<PyString>() {
        return Ok(Value::String(text.extract::<String>()?.into()));
    }
    if let Ok(data) = object.cast::<PyBytes>() {
        return Ok(Value::Binary(data.as_bytes().to_vec()));
    }
    if let Ok(mapping) = object.cast::<PyDict>() {
        let mut entries = Vec::with_capacity(mapping.len());
        for (key, value) in mapping.iter() {
            entries.push((to_value(&key)?, to_value(&value)?));
        }
        return Ok(Value::Map(entries));
    }
    if let Ok(items) = object.cast::<PyList>() {
        return Ok(Value::Array(
            items
                .iter()
                .map(|item| to_value(&item))
                .collect::<PyResult<_>>()?,
        ));
    }
    if let Ok(items) = object.cast::<PyTuple>() {
        return Ok(Value::Array(
            items
                .iter()
                .map(|item| to_value(&item))
                .collect::<PyResult<_>>()?,
        ));
    }

    Err(PyTypeError::new_err(format!(
        "{} cannot be sent through a channel; use None, bool, int, float, str, bytes, list, tuple or dict",
        object.get_type().name()?
    )))
}

/// Convert a decoded message back into Python.
fn to_python(python: Python<'_>, value: &Value) -> PyResult<Py<PyAny>> {
    Ok(match value {
        Value::Nil => python.None(),
        Value::Boolean(boolean) => boolean
            .into_pyobject(python)?
            .to_owned()
            .into_any()
            .unbind(),
        Value::Integer(integer) => match integer.as_i64() {
            Some(number) => number.into_pyobject(python)?.into_any().unbind(),
            // Values beyond a signed 64-bit integer still fit Python's, which
            // has no such limit.
            None => match integer.as_u64() {
                Some(number) => number.into_pyobject(python)?.into_any().unbind(),
                None => return Err(PyRuntimeError::new_err("an integer could not be read")),
            },
        },
        Value::F32(number) => number.into_pyobject(python)?.into_any().unbind(),
        Value::F64(number) => number.into_pyobject(python)?.into_any().unbind(),
        Value::String(text) => match text.as_str() {
            Some(text) => text.into_pyobject(python)?.into_any().unbind(),
            // A string field that is not valid UTF-8 arrives as bytes rather
            // than being replaced or dropped, so nothing is silently lost.
            None => PyBytes::new(python, text.as_bytes()).into_any().unbind(),
        },
        Value::Binary(data) => PyBytes::new(python, data).into_any().unbind(),
        Value::Array(items) => {
            let converted: Vec<Py<PyAny>> = items
                .iter()
                .map(|item| to_python(python, item))
                .collect::<PyResult<_>>()?;
            PyList::new(python, converted)?.into_any().unbind()
        }
        Value::Map(entries) => {
            let mapping = PyDict::new(python);
            for (key, value) in entries {
                mapping.set_item(to_python(python, key)?, to_python(python, value)?)?;
            }
            mapping.into_any().unbind()
        }
        Value::Ext(tag, data) => {
            let pair = (*tag, PyBytes::new(python, data));
            pair.into_pyobject(python)?.into_any().unbind()
        }
    })
}

fn encode(object: &Bound<'_, PyAny>) -> PyResult<Bytes> {
    codec::encode(&to_value(object)?).map_err(|error| PyRuntimeError::new_err(error.to_string()))
}

fn decode(python: Python<'_>, payload: &[u8]) -> PyResult<Py<PyAny>> {
    let value =
        codec::decode(payload).map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
    to_python(python, &value)
}

// ── the client ───────────────────────────────────────────────────────────────

/// A connection to the message store.
#[pyclass(name = "Intercom", module = "nitro._nitro")]
pub struct Intercom {
    inner: Arc<CoreIntercom>,
}

#[pymethods]
impl Intercom {
    /// `await Intercom.connect(url)` — connect to the message store.
    #[staticmethod]
    #[pyo3(signature = (url, *, prefix=String::new(), capacity=100, expiry=60.0))]
    fn connect<'py>(
        python: Python<'py>,
        url: String,
        prefix: String,
        capacity: usize,
        expiry: f64,
    ) -> PyResult<Bound<'py, PyAny>> {
        let config = ChannelConfig {
            prefix,
            capacity,
            expiry: std::time::Duration::from_secs_f64(expiry.max(0.0)),
        };

        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let inner = CoreIntercom::connect(&url, config)
                .await
                .map_err(intercom_error)?;
            Python::attach(|python| {
                Py::new(
                    python,
                    Intercom {
                        inner: Arc::new(inner),
                    },
                )
            })
        })
    }

    /// A channel name nothing else is using.
    #[staticmethod]
    #[pyo3(signature = (prefix="channel"))]
    fn new_channel(prefix: &str) -> String {
        unique_channel(prefix)
    }

    /// `await intercom.publish(channel, message)` — deliver to whoever is
    /// listening right now, returning how many that was.
    fn publish<'py>(
        &self,
        python: Python<'py>,
        channel: String,
        message: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let payload = encode(message)?;
        let inner = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            inner
                .publish(&channel, payload)
                .await
                .map_err(intercom_error)
        })
    }

    /// `await intercom.subscribe(channel)` — listen to a channel.
    fn subscribe<'py>(&self, python: Python<'py>, channel: String) -> PyResult<Bound<'py, PyAny>> {
        let inner = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let subscription = inner.subscribe(&channel).await.map_err(intercom_error)?;
            Python::attach(|python| {
                Py::new(
                    python,
                    Listener {
                        subscription: Arc::new(tokio::sync::Mutex::new(Some(subscription))),
                    },
                )
            })
        })
    }

    /// `await intercom.send(channel, message)` — queue a message for whoever
    /// reads the channel next.
    fn send<'py>(
        &self,
        python: Python<'py>,
        channel: String,
        message: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let payload = encode(message)?;
        let inner = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            inner.send(&channel, payload).await.map_err(intercom_error)
        })
    }

    /// `await intercom.receive(channel)` — the oldest queued message, or `None`.
    fn receive<'py>(&self, python: Python<'py>, channel: String) -> PyResult<Bound<'py, PyAny>> {
        let inner = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            match inner.receive(&channel).await.map_err(intercom_error)? {
                Some(payload) => Python::attach(|python| decode(python, &payload)),
                None => Python::attach(|python| Ok(python.None())),
            }
        })
    }

    /// `await intercom.reader(channel)` — a reader that can wait for messages.
    fn reader<'py>(&self, python: Python<'py>, channel: String) -> PyResult<Bound<'py, PyAny>> {
        let inner = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let reader = inner.reader(&channel).await.map_err(intercom_error)?;
            Python::attach(|python| {
                Py::new(
                    python,
                    Reader {
                        reader: Arc::new(tokio::sync::Mutex::new(reader)),
                    },
                )
            })
        })
    }

    fn group_add<'py>(
        &self,
        python: Python<'py>,
        group: String,
        channel: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            inner
                .group_add(&group, &channel)
                .await
                .map_err(intercom_error)
        })
    }

    fn group_discard<'py>(
        &self,
        python: Python<'py>,
        group: String,
        channel: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            inner
                .group_discard(&group, &channel)
                .await
                .map_err(intercom_error)
        })
    }

    fn group_channels<'py>(
        &self,
        python: Python<'py>,
        group: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let inner = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            inner.group_channels(&group).await.map_err(intercom_error)
        })
    }

    /// `await intercom.group_send(group, message)` — queue for every member.
    fn group_send<'py>(
        &self,
        python: Python<'py>,
        group: String,
        message: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let payload = encode(message)?;
        let inner = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            inner
                .group_send(&group, payload)
                .await
                .map_err(intercom_error)
        })
    }

    /// `await intercom.group_publish(group, message)` — deliver to every member
    /// listening right now.
    fn group_publish<'py>(
        &self,
        python: Python<'py>,
        group: String,
        message: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let payload = encode(message)?;
        let inner = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            inner
                .group_publish(&group, payload)
                .await
                .map_err(intercom_error)
        })
    }

    /// `await intercom.flush()` — remove every channel and group under this
    /// client's prefix.
    fn flush<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            inner.flush().await.map_err(intercom_error)
        })
    }

    /// `await intercom.ping()` — check the store is reachable.
    fn ping<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            inner.ping().await.map_err(intercom_error)
        })
    }

    fn __repr__(&self) -> String {
        format!("Intercom(prefix={:?})", self.inner.config().prefix)
    }
}

/// A live subscription, iterable with `async for`.
#[pyclass(name = "IntercomListener", module = "nitro._nitro")]
pub struct Listener {
    subscription: Arc<tokio::sync::Mutex<Option<Subscription>>>,
}

#[pymethods]
impl Listener {
    fn __aiter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __anext__<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let subscription = Arc::clone(&self.subscription);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let mut guard = subscription.lock().await;
            let Some(active) = guard.as_mut() else {
                return Err(PyStopAsyncIteration::new_err(()));
            };
            match active.next_message().await {
                Some(payload) => Python::attach(|python| decode(python, &payload)),
                None => {
                    *guard = None;
                    Err(PyStopAsyncIteration::new_err(()))
                }
            }
        })
    }

    /// `await listener.receive()` — the next message, or `None` once the
    /// subscription has ended.
    fn receive<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let subscription = Arc::clone(&self.subscription);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            let mut guard = subscription.lock().await;
            let Some(active) = guard.as_mut() else {
                return Python::attach(|python| Ok(python.None()));
            };
            match active.next_message().await {
                Some(payload) => Python::attach(|python| decode(python, &payload)),
                None => {
                    *guard = None;
                    Python::attach(|python| Ok(python.None()))
                }
            }
        })
    }

    /// `await listener.close()` — stop listening.
    fn close<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let subscription = Arc::clone(&self.subscription);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            subscription.lock().await.take();
            Ok(())
        })
    }
}

/// A queued-channel reader with a connection of its own.
#[pyclass(name = "IntercomReader", module = "nitro._nitro")]
pub struct Reader {
    reader: Arc<tokio::sync::Mutex<ChannelReader>>,
}

#[pymethods]
impl Reader {
    /// `await reader.receive(timeout)` — the oldest queued message, waiting up
    /// to `timeout` seconds. Zero waits indefinitely.
    #[pyo3(signature = (timeout=0.0))]
    fn receive<'py>(&self, python: Python<'py>, timeout: f64) -> PyResult<Bound<'py, PyAny>> {
        let reader = Arc::clone(&self.reader);
        let timeout = std::time::Duration::from_secs_f64(timeout.max(0.0));

        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            match reader
                .lock()
                .await
                .next_message(timeout)
                .await
                .map_err(intercom_error)?
            {
                Some(payload) => Python::attach(|python| decode(python, &payload)),
                None => Python::attach(|python| Ok(python.None())),
            }
        })
    }

    /// `await reader.try_receive()` — a queued message if one is already there.
    fn try_receive<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let reader = Arc::clone(&self.reader);
        pyo3_async_runtimes::tokio::future_into_py(python, async move {
            match reader
                .lock()
                .await
                .try_next_message()
                .await
                .map_err(intercom_error)?
            {
                Some(payload) => Python::attach(|python| decode(python, &payload)),
                None => Python::attach(|python| Ok(python.None())),
            }
        })
    }
}
