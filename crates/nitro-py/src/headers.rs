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

//! The Python-facing header map.

use std::sync::Arc;

use nitro_core::headers::Headers as CoreHeaders;
use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};

/// A read-only, dict-like view of a request's headers.
///
/// Iteration and `len` work on names, while `items` and `values` work on
/// entries, so a name that appears twice contributes one key and two items.
/// That difference is intentional and is documented on the underlying map.
#[pyclass(name = "Headers", module = "nitro._nitro", frozen)]
pub struct Headers {
    inner: Arc<CoreHeaders>,
}

impl Headers {
    pub fn new(inner: CoreHeaders) -> Self {
        Self {
            inner: Arc::new(inner),
        }
    }

    pub fn core(&self) -> &CoreHeaders {
        &self.inner
    }
}

#[pymethods]
impl Headers {
    /// The first value for `name`, raising `KeyError` when it is absent.
    fn __getitem__(&self, name: &str) -> PyResult<String> {
        self.inner
            .get(name)
            .map(str::to_owned)
            .ok_or_else(|| PyKeyError::new_err(name.to_owned()))
    }

    /// The first value for `name`, or `default` when it is absent.
    #[pyo3(signature = (name, default=None))]
    fn get(&self, name: &str, default: Option<String>) -> Option<String> {
        self.inner.get(name).map(str::to_owned).or(default)
    }

    /// Every value for `name`, in the order received.
    fn get_all(&self, name: &str) -> Vec<String> {
        self.inner
            .get_all(name)
            .into_iter()
            .map(str::to_owned)
            .collect()
    }

    /// Every entry as a `(name, value)` pair, repeating a name once per value.
    fn items<'py>(&self, python: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let items = self
            .inner
            .items()
            .into_iter()
            .map(|(name, value)| PyTuple::new(python, [name, value]))
            .collect::<PyResult<Vec<_>>>()?;
        PyList::new(python, items)
    }

    /// Each distinct name exactly once.
    fn keys(&self) -> Vec<String> {
        self.inner.keys().into_iter().map(str::to_owned).collect()
    }

    /// Every value, one per entry.
    fn values(&self) -> Vec<String> {
        self.inner.values().into_iter().map(str::to_owned).collect()
    }

    fn __contains__(&self, name: &str) -> bool {
        self.inner.contains(name)
    }

    fn __iter__(&self, python: Python<'_>) -> PyResult<Py<PyAny>> {
        let keys = PyList::new(python, self.keys())?;
        Ok(keys.try_iter()?.into_any().unbind())
    }

    /// The number of distinct names.
    fn __len__(&self) -> usize {
        self.inner.len()
    }

    fn __repr__(&self) -> String {
        let entries: Vec<String> = self
            .inner
            .items()
            .into_iter()
            .map(|(name, value)| format!("{name:?}: {value:?}"))
            .collect();
        format!("Headers({{{}}})", entries.join(", "))
    }
}
