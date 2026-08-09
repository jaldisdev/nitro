//! Python bindings for `intercom-core`.

use pyo3::prelude::*;

#[pymodule]
fn _intercom(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add(
        "__doc__",
        "Compiled publish/subscribe backend for Intercom.",
    )?;
    Ok(())
}
