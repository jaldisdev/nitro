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

//! Python bindings for the Nitro server core.

use pyo3::prelude::*;

mod config;
mod dispatch;
mod handoff;
mod headers;
mod intercom_bridge;
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

    module.add_class::<intercom_bridge::Intercom>()?;
    module.add_class::<intercom_bridge::Listener>()?;
    module.add_class::<intercom_bridge::Reader>()?;

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
