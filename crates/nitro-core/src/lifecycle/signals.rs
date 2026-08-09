//! Turning operating-system signals into an awaitable shutdown request.
//!
//! The signal handler itself does nothing but record that a signal arrived —
//! anything more would have to be async-signal-safe. The recorded flag is
//! published through a watch channel, which is what the rest of the server
//! actually waits on, so any number of tasks can observe the same request and a
//! task that starts waiting after the fact still sees it.

use std::io;

use tokio::sync::watch;

/// Broadcasts a shutdown request to every part of a worker.
#[derive(Debug)]
pub struct ShutdownController {
    sender: watch::Sender<bool>,
}

impl Default for ShutdownController {
    fn default() -> Self {
        Self::new()
    }
}

impl ShutdownController {
    pub fn new() -> Self {
        let (sender, _receiver) = watch::channel(false);
        Self { sender }
    }

    pub fn subscribe(&self) -> ShutdownSignal {
        ShutdownSignal {
            receiver: self.sender.subscribe(),
            keepalive: None,
        }
    }

    /// Request shutdown. Repeated calls are harmless.
    pub fn trigger(&self) {
        self.sender.send_replace(true);
    }

    pub fn is_triggered(&self) -> bool {
        *self.sender.borrow()
    }

    /// Watch for termination signals for the lifetime of the returned task.
    ///
    /// Each signal kind gets its own listener rather than one combined loop, so
    /// a signal arriving while another is being handled is never missed.
    #[cfg(unix)]
    pub fn watch_termination_signals(&self) -> io::Result<tokio::task::JoinHandle<()>> {
        use tokio::signal::unix::{SignalKind, signal};

        let mut interrupt = signal(SignalKind::interrupt())?;
        let mut terminate = signal(SignalKind::terminate())?;
        let sender = self.sender.clone();

        Ok(tokio::spawn(async move {
            let received = tokio::select! {
                _ = interrupt.recv() => "SIGINT",
                _ = terminate.recv() => "SIGTERM",
            };
            tracing::info!(signal = received, "shutdown requested");
            sender.send_replace(true);
        }))
    }

    #[cfg(not(unix))]
    pub fn watch_termination_signals(&self) -> io::Result<tokio::task::JoinHandle<()>> {
        let sender = self.sender.clone();
        Ok(tokio::spawn(async move {
            if tokio::signal::ctrl_c().await.is_ok() {
                tracing::info!(signal = "CTRL_C", "shutdown requested");
                sender.send_replace(true);
            }
        }))
    }
}

/// The receiving side of a shutdown request.
#[derive(Debug, Clone)]
pub struct ShutdownSignal {
    receiver: watch::Receiver<bool>,
    /// Only set by [`ShutdownSignal::never`], which has no controller of its
    /// own. Holding the sender is what keeps the channel open, so a wait parks
    /// forever instead of resolving with a closed-channel error.
    #[allow(dead_code, reason = "held for its lifetime, never read")]
    keepalive: Option<std::sync::Arc<watch::Sender<bool>>>,
}

impl ShutdownSignal {
    /// A signal that is never triggered, for tests and for embedding contexts
    /// that manage their own lifetime.
    pub fn never() -> Self {
        let (sender, receiver) = watch::channel(false);
        Self {
            receiver,
            keepalive: Some(std::sync::Arc::new(sender)),
        }
    }

    pub fn is_triggered(&self) -> bool {
        *self.receiver.borrow()
    }

    /// Resolve once shutdown has been requested, immediately if it already was.
    pub async fn wait(&self) {
        let mut receiver = self.receiver.clone();
        if *receiver.borrow_and_update() {
            return;
        }
        // A closed channel means the controller is gone, which can only happen
        // while the server is being torn down — treat it as a request.
        if receiver.changed().await.is_err() {
            tracing::debug!("shutdown controller dropped before signalling");
        }
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;

    #[tokio::test]
    async fn a_trigger_reaches_every_subscriber() {
        let controller = ShutdownController::new();
        let waiters: Vec<_> = (0..4)
            .map(|_| {
                let signal = controller.subscribe();
                tokio::spawn(async move { signal.wait().await })
            })
            .collect();

        tokio::task::yield_now().await;
        controller.trigger();

        for waiter in waiters {
            tokio::time::timeout(Duration::from_secs(1), waiter)
                .await
                .expect("every subscriber must observe the request")
                .expect("waiter task panicked");
        }
    }

    #[tokio::test]
    async fn subscribing_after_the_trigger_still_observes_it() {
        let controller = ShutdownController::new();
        controller.trigger();

        let signal = controller.subscribe();
        assert!(signal.is_triggered());
        tokio::time::timeout(Duration::from_millis(50), signal.wait())
            .await
            .expect("a late subscriber must not park");
    }

    #[tokio::test]
    async fn triggering_twice_is_harmless() {
        let controller = ShutdownController::new();
        controller.trigger();
        controller.trigger();
        assert!(controller.is_triggered());
    }

    #[tokio::test]
    async fn dropping_the_controller_releases_waiters() {
        let controller = ShutdownController::new();
        let signal = controller.subscribe();
        drop(controller);

        tokio::time::timeout(Duration::from_millis(50), signal.wait())
            .await
            .expect("a lost controller must not strand the server");
    }

    #[tokio::test]
    async fn the_never_signal_does_not_resolve() {
        let signal = ShutdownSignal::never();
        assert!(!signal.is_triggered());
        assert!(
            tokio::time::timeout(Duration::from_millis(50), signal.wait())
                .await
                .is_err()
        );
    }
}
