//! Python bindings for `intercom-core`.

use pyo3::prelude::*;

mod bindings;

#[pymodule]
fn _intercom(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add(
        "__doc__",
        "Compiled publish/subscribe backend for Intercom.",
    )?;

    module.add_class::<bindings::Intercom>()?;
    module.add_class::<bindings::Listener>()?;
    module.add_class::<bindings::Reader>()?;

    Ok(())
}
