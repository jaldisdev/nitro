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

//! Request metadata handed to a handler.
//!
//! Attributes are read directly off the object rather than looked up in a
//! dictionary, so a typo is an `AttributeError` at the point of use and the
//! shape of a request is discoverable.

use std::net::SocketAddr;

use nitro_core::router::RouteMatch;
use nitro_core::transport::RequestParts;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

use crate::headers::Headers;

fn address_pair(address: Option<SocketAddr>) -> Option<(String, u16)> {
    address.map(|address| (address.ip().to_string(), address.port()))
}

#[pyclass(name = "HttpScope", module = "nitro._nitro", frozen)]
pub struct HttpScope {
    #[pyo3(get)]
    pub proto: &'static str,
    #[pyo3(get)]
    pub http_version: &'static str,
    #[pyo3(get)]
    pub method: String,
    #[pyo3(get)]
    pub path: String,
    #[pyo3(get)]
    pub query_string: String,
    #[pyo3(get)]
    pub scheme: &'static str,
    #[pyo3(get)]
    pub authority: Option<String>,
    /// The address the request arrived on, absent on a Unix domain socket.
    #[pyo3(get)]
    pub server: Option<(String, u16)>,
    /// The peer's address, absent on a Unix domain socket.
    #[pyo3(get)]
    pub client: Option<(String, u16)>,
    #[pyo3(get)]
    pub headers: Py<Headers>,
    /// The route that answers this request, or `None` when none does.
    #[pyo3(get)]
    pub route_id: Option<u64>,
    /// Captured path parameters, as the text they were captured from.
    /// Converting them to Python values is the application's job.
    #[pyo3(get)]
    pub path_params: Py<PyDict>,
    /// When no route answers this method but some route answers the path, the
    /// methods that would have worked. Empty otherwise.
    #[pyo3(get)]
    pub allowed_methods: Py<PyTuple>,
}

impl HttpScope {
    /// Takes the request's parts rather than borrowing them, so the headers can
    /// be moved into the scope. Cloning them was a copy of the whole map on
    /// every request, paid whether or not the handler ever read a header, and
    /// nothing on the Rust side reads them once the scope exists.
    pub fn from_parts(
        python: Python<'_>,
        parts: RequestParts,
        matched: &RouteMatch,
    ) -> PyResult<Self> {
        let path_params = PyDict::new(python);
        let mut route_id = None;
        let mut allowed: Vec<&str> = Vec::new();

        match matched {
            RouteMatch::Found {
                route_id: identifier,
                parameters,
            } => {
                route_id = Some(*identifier);
                for (name, value) in parameters {
                    path_params.set_item(name, value)?;
                }
            }
            RouteMatch::MethodNotAllowed { allowed: methods } => {
                allowed = methods.iter().map(String::as_str).collect();
            }
            RouteMatch::NotFound => {}
        }

        Ok(Self {
            proto: "http",
            http_version: parts.http_version(),
            method: parts.method.as_str().to_owned(),
            path: parts.path().to_owned(),
            query_string: parts.query().to_owned(),
            scheme: parts.scheme.as_str(),
            authority: parts.authority().map(str::to_owned),
            server: address_pair(parts.server),
            client: address_pair(parts.client),
            headers: Py::new(python, Headers::new(parts.headers))?,
            route_id,
            path_params: path_params.unbind(),
            allowed_methods: PyTuple::new(python, allowed)?.unbind(),
        })
    }
}

#[pymethods]
impl HttpScope {
    fn __repr__(&self) -> String {
        format!(
            "HttpScope(method={:?}, path={:?}, http_version={:?})",
            self.method, self.path, self.http_version
        )
    }
}

/// Request metadata for a WebSocket upgrade.
///
/// The shape follows the HTTP scope so a handler can read the same attributes,
/// with the offered subprotocols added and the body-related ones left out.
#[pyclass(name = "WsScope", module = "nitro._nitro", frozen)]
pub struct WsScope {
    #[pyo3(get)]
    pub proto: &'static str,
    #[pyo3(get)]
    pub http_version: &'static str,
    #[pyo3(get)]
    pub path: String,
    #[pyo3(get)]
    pub query_string: String,
    #[pyo3(get)]
    pub scheme: &'static str,
    #[pyo3(get)]
    pub authority: Option<String>,
    #[pyo3(get)]
    pub server: Option<(String, u16)>,
    #[pyo3(get)]
    pub client: Option<(String, u16)>,
    #[pyo3(get)]
    pub headers: Py<Headers>,
    #[pyo3(get)]
    pub subprotocols: Py<PyTuple>,
    #[pyo3(get)]
    pub route_id: Option<u64>,
    #[pyo3(get)]
    pub path_params: Py<PyDict>,
}

impl WsScope {
    pub fn from_parts(
        python: Python<'_>,
        parts: &RequestParts,
        matched: &RouteMatch,
        subprotocols: &[String],
    ) -> PyResult<Self> {
        let path_params = PyDict::new(python);
        let mut route_id = None;

        if let RouteMatch::Found {
            route_id: identifier,
            parameters,
        } = matched
        {
            route_id = Some(*identifier);
            for (name, value) in parameters {
                path_params.set_item(name, value)?;
            }
        }

        Ok(Self {
            proto: "websocket",
            http_version: parts.http_version(),
            path: parts.path().to_owned(),
            query_string: parts.query().to_owned(),
            scheme: if parts.scheme.is_secure() {
                "wss"
            } else {
                "ws"
            },
            authority: parts.authority().map(str::to_owned),
            server: address_pair(parts.server),
            client: address_pair(parts.client),
            headers: Py::new(python, Headers::new(parts.headers.clone()))?,
            subprotocols: PyTuple::new(python, subprotocols)?.unbind(),
            route_id,
            path_params: path_params.unbind(),
        })
    }
}

#[pymethods]
impl WsScope {
    fn __repr__(&self) -> String {
        format!("WsScope(path={:?}, scheme={:?})", self.path, self.scheme)
    }
}

/// Request metadata for a WebTransport session.
#[pyclass(name = "WtScope", module = "nitro._nitro", frozen)]
pub struct WtScope {
    #[pyo3(get)]
    pub proto: &'static str,
    #[pyo3(get)]
    pub path: String,
    #[pyo3(get)]
    pub query_string: String,
    #[pyo3(get)]
    pub authority: Option<String>,
    #[pyo3(get)]
    pub server: Option<(String, u16)>,
    #[pyo3(get)]
    pub client: Option<(String, u16)>,
    #[pyo3(get)]
    pub headers: Py<Headers>,
    #[pyo3(get)]
    pub route_id: Option<u64>,
    #[pyo3(get)]
    pub path_params: Py<PyDict>,
}

impl WtScope {
    pub fn from_parts(
        python: Python<'_>,
        parts: &RequestParts,
        matched: &RouteMatch,
    ) -> PyResult<Self> {
        let path_params = PyDict::new(python);
        let mut route_id = None;

        if let RouteMatch::Found {
            route_id: identifier,
            parameters,
        } = matched
        {
            route_id = Some(*identifier);
            for (name, value) in parameters {
                path_params.set_item(name, value)?;
            }
        }

        Ok(Self {
            proto: "webtransport",
            path: parts.path().to_owned(),
            query_string: parts.query().to_owned(),
            authority: parts.authority().map(str::to_owned),
            server: address_pair(parts.server),
            client: address_pair(parts.client),
            headers: Py::new(python, Headers::new(parts.headers.clone()))?,
            route_id,
            path_params: path_params.unbind(),
        })
    }
}

#[pymethods]
impl WtScope {
    fn __repr__(&self) -> String {
        format!("WtScope(path={:?})", self.path)
    }
}
