//! Python bindings for the Nitro server core.

use pyo3::prelude::*;

mod config;
mod dispatch;
mod headers;
mod lifecycle;
mod protocol;
mod scope;
mod server;

#[pymodule]
fn _nitro(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("__doc__", "Compiled server core for the Nitro framework.")?;

    module.add_class::<server::Server>()?;
    module.add_class::<scope::HttpScope>()?;
    module.add_class::<headers::Headers>()?;
    module.add_class::<protocol::HttpProtocol>()?;
    module.add_class::<protocol::StreamTransport>()?;
    module.add_class::<scope::WsScope>()?;
    module.add_class::<protocol::WsTransport>()?;
    module.add_class::<scope::WtScope>()?;
    module.add_class::<protocol::WtSession>()?;
    module.add_class::<protocol::WtStream>()?;

    module.add("HTTP_ENTRY_POINT", dispatch::HTTP_ENTRY_POINT)?;
    module.add("WEBSOCKET_ENTRY_POINT", dispatch::WEBSOCKET_ENTRY_POINT)?;
    module.add("WEBSOCKET_METHOD", dispatch::WEBSOCKET_METHOD)?;
    module.add(
        "WEBTRANSPORT_ENTRY_POINT",
        dispatch::WEBTRANSPORT_ENTRY_POINT,
    )?;
    module.add("WEBTRANSPORT_METHOD", dispatch::WEBTRANSPORT_METHOD)?;
    module.add("STARTUP_HOOK", lifecycle::STARTUP_HOOK)?;
    module.add("SHUTDOWN_HOOK", lifecycle::SHUTDOWN_HOOK)?;

    Ok(())
}
