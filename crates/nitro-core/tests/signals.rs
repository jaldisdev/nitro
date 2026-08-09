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
