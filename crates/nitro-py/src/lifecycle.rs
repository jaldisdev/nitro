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

//! The application's startup and shutdown hooks.
//!
//! Both are synchronous and receive the worker's event loop. `__startup__` runs
//! with the loop constructed but not yet running, so an application that needs
//! to do asynchronous initialisation can drive it with `run_until_complete`
//! before the first connection is accepted. `__shutdown__` runs after the loop
//! has stopped, under the same rule.
//!
//! Neither hook is required.

use pyo3::prelude::*;

pub const STARTUP_HOOK: &str = "__startup__";
pub const SHUTDOWN_HOOK: &str = "__shutdown__";

fn call_hook(
    application: &Bound<'_, PyAny>,
    event_loop: &Bound<'_, PyAny>,
    name: &str,
) -> PyResult<bool> {
    if !application.hasattr(name)? {
        return Ok(false);
    }
    application.call_method1(name, (event_loop,))?;
    tracing::debug!(hook = name, "application hook completed");
    Ok(true)
}

pub fn call_startup(application: &Bound<'_, PyAny>, event_loop: &Bound<'_, PyAny>) -> PyResult<()> {
    call_hook(application, event_loop, STARTUP_HOOK).map(|_| ())
}

/// Run the shutdown hook, reporting a failure without letting it stop the rest
/// of the teardown — the caller still has an event loop to close.
pub fn call_shutdown(
    application: &Bound<'_, PyAny>,
    event_loop: &Bound<'_, PyAny>,
) -> Option<PyErr> {
    match call_hook(application, event_loop, SHUTDOWN_HOOK) {
        Ok(_) => None,
        Err(error) => {
            let python = application.py();
            tracing::error!(error = %error.value(python), "the shutdown hook raised");
            Some(error)
        }
    }
}
