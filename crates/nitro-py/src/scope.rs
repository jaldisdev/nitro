//! Request metadata handed to a handler.
//!
//! Attributes are read directly off the object rather than looked up in a
//! dictionary, so a typo is an `AttributeError` at the point of use and the
//! shape of a request is discoverable.

use std::net::SocketAddr;

use nitro_core::transport::RequestParts;
use pyo3::prelude::*;

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
}

impl HttpScope {
    pub fn from_parts(python: Python<'_>, parts: &RequestParts) -> PyResult<Self> {
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
            headers: Py::new(python, Headers::new(parts.headers.clone()))?,
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
