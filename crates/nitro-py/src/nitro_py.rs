//! Python bindings for the Nitro server core.

use pyo3::prelude::*;

#[pymodule]
fn _nitro(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("__doc__", "Compiled server core for the Nitro framework.")?;
    Ok(())
}
