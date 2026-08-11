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

//! Signal delivery, in its own test binary because raising a signal affects the
//! whole process.

#![cfg(unix)]

use std::time::Duration;

use nitro_core::lifecycle::signals::ShutdownController;

#[tokio::test]
async fn sigterm_requests_shutdown() {
    let controller = ShutdownController::new();
    let signal = controller.subscribe();
    let watcher = controller
        .watch_termination_signals()
        .expect("installing signal handlers must work");

    // The handler replaces the default disposition, which would otherwise end
    // the test process here.
    unsafe { libc::raise(libc::SIGTERM) };

    tokio::time::timeout(Duration::from_secs(5), signal.wait())
        .await
        .expect("SIGTERM must reach the shutdown signal");
    assert!(controller.is_triggered());

    watcher.await.expect("the watcher task must finish cleanly");
}
