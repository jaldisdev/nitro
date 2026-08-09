//! Client disconnect notification.
//!
//! A handler that produces a long-lived response needs to know when there is no
//! longer anybody to send it to. The scope of that notification is the
//! connection: it fires when the connection is torn down for any reason, and
//! when the server begins draining. There is no half-close detection — a client
//! that shuts down only its write side is still considered connected.
//!
//! Reacting to the notification is the application's job. Awaiting it tells a
//! handler to stop; nothing here cancels a handler's task on its behalf.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use tokio::sync::Notify;

#[derive(Debug, Default)]
struct Inner {
    notify: Notify,
    triggered: AtomicBool,
}

/// A one-way latch that any number of tasks can await.
#[derive(Debug, Clone, Default)]
pub struct DisconnectSignal {
    inner: Arc<Inner>,
}

impl DisconnectSignal {
    pub fn new() -> Self {
        Self::default()
    }

    /// Release every waiter. Calling this more than once is harmless.
    pub fn trigger(&self) {
        // The flag is published before the wake-up so that a task which checks
        // it between the two operations sees the trigger and does not park.
        self.inner.triggered.store(true, Ordering::Release);
        // Every waiter is woken, not just one: a single HTTP/2 connection can
        // carry many concurrent streams whose handlers are all awaiting this,
        // and waking one of them would strand the others.
        self.inner.notify.notify_waiters();
    }

    pub fn is_triggered(&self) -> bool {
        self.inner.triggered.load(Ordering::Acquire)
    }

    /// Resolve once the signal has been triggered, immediately if that already
    /// happened.
    pub async fn wait(&self) {
        if self.is_triggered() {
            return;
        }
        // Registration happens before the second check so that a trigger racing
        // with this call is caught by one or the other.
        let notified = self.inner.notify.notified();
        if self.is_triggered() {
            return;
        }
        notified.await;
    }
}

/// Owned by whichever task drives a connection. Dropping it — on clean close,
/// on error, or on panic — reports the disconnect.
#[derive(Debug)]
pub struct DisconnectGuard {
    signal: DisconnectSignal,
}

impl Default for DisconnectGuard {
    fn default() -> Self {
        Self::new()
    }
}

impl DisconnectGuard {
    pub fn new() -> Self {
        Self {
            signal: DisconnectSignal::new(),
        }
    }

    pub fn signal(&self) -> DisconnectSignal {
        self.signal.clone()
    }

    /// A watcher for handlers running on this connection, which also resolves
    /// when the server starts draining.
    pub fn watcher(&self, server_drain: DisconnectSignal) -> DisconnectWatcher {
        DisconnectWatcher {
            connection: self.signal.clone(),
            server_drain: Some(server_drain),
        }
    }
}

impl Drop for DisconnectGuard {
    fn drop(&mut self) {
        self.signal.trigger();
    }
}

/// Handed to request handlers. Resolves on connection teardown or on server
/// drain, whichever happens first.
#[derive(Debug, Clone)]
pub struct DisconnectWatcher {
    connection: DisconnectSignal,
    server_drain: Option<DisconnectSignal>,
}

impl DisconnectWatcher {
    pub fn new(connection: DisconnectSignal, server_drain: DisconnectSignal) -> Self {
        Self {
            connection,
            server_drain: Some(server_drain),
        }
    }

    /// A watcher that only tracks the connection, for contexts with no drain
    /// coordination such as unit tests.
    pub fn connection_only(connection: DisconnectSignal) -> Self {
        Self {
            connection,
            server_drain: None,
        }
    }

    pub fn is_disconnected(&self) -> bool {
        self.connection.is_triggered()
            || self
                .server_drain
                .as_ref()
                .is_some_and(DisconnectSignal::is_triggered)
    }

    pub async fn wait(&self) {
        match &self.server_drain {
            Some(drain) => {
                tokio::select! {
                    () = self.connection.wait() => {}
                    () = drain.wait() => {}
                }
            }
            None => self.connection.wait().await,
        }
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;

    #[tokio::test]
    async fn waiting_after_the_trigger_returns_immediately() {
        let signal = DisconnectSignal::new();
        signal.trigger();
        tokio::time::timeout(Duration::from_millis(50), signal.wait())
            .await
            .expect("a signal triggered before the wait must not park the caller");
    }

    #[tokio::test]
    async fn every_waiter_is_released() {
        let signal = DisconnectSignal::new();
        let waiters: Vec<_> = (0..8)
            .map(|_| {
                let signal = signal.clone();
                tokio::spawn(async move { signal.wait().await })
            })
            .collect();

        tokio::task::yield_now().await;
        signal.trigger();

        for waiter in waiters {
            tokio::time::timeout(Duration::from_secs(1), waiter)
                .await
                .expect("all waiters must wake, not just one")
                .expect("waiter task panicked");
        }
    }

    #[tokio::test]
    async fn dropping_the_guard_notifies() {
        let guard = DisconnectGuard::new();
        let signal = guard.signal();
        assert!(!signal.is_triggered());

        let waiter = {
            let signal = signal.clone();
            tokio::spawn(async move { signal.wait().await })
        };
        tokio::task::yield_now().await;
        drop(guard);

        tokio::time::timeout(Duration::from_secs(1), waiter)
            .await
            .expect("dropping the guard must release waiters")
            .expect("waiter task panicked");
        assert!(signal.is_triggered());
    }

    #[tokio::test]
    async fn a_watcher_also_follows_the_server_drain() {
        let guard = DisconnectGuard::new();
        let drain = DisconnectSignal::new();
        let watcher = guard.watcher(drain.clone());

        assert!(!watcher.is_disconnected());
        drain.trigger();

        assert!(watcher.is_disconnected());
        tokio::time::timeout(Duration::from_millis(50), watcher.wait())
            .await
            .expect("draining must release connection watchers");
    }

    #[tokio::test]
    async fn triggering_twice_is_harmless() {
        let signal = DisconnectSignal::new();
        signal.trigger();
        signal.trigger();
        assert!(signal.is_triggered());
    }
}
