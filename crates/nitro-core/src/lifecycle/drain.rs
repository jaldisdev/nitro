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

//! Graceful shutdown.
//!
//! Draining runs as a fixed chain, and the order matters:
//!
//! 1. every accept loop returns, so no new connection is taken on;
//! 2. handlers waiting on a disconnect are released, which is how a long-lived
//!    response learns to wind down instead of being cut off mid-frame;
//! 3. every open connection is asked to finish its current exchange and close;
//! 4. tasks spawned outside the connection machinery are awaited.
//!
//! Steps 3 and 4 share one deadline. Whatever has not finished by then is
//! abandoned, because a shutdown that one stuck handler can delay indefinitely
//! is not a shutdown.

use std::time::{Duration, Instant};

use tokio::time::timeout_at;
use tokio_util::task::TaskTracker;

use crate::disconnect::DisconnectSignal;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DrainOutcome {
    /// Every connection closed within the deadline.
    pub connections_finished: bool,
    /// Every background task finished within the deadline.
    pub tasks_finished: bool,
}

impl DrainOutcome {
    pub fn is_complete(self) -> bool {
        self.connections_finished && self.tasks_finished
    }
}

/// Owns the shutdown chain and the handles it needs to run it.
#[derive(Debug, Clone)]
pub struct DrainCoordinator {
    handlers: DisconnectSignal,
    graceful: DisconnectSignal,
    connections: TaskTracker,
    background: TaskTracker,
    timeout: Duration,
}

impl DrainCoordinator {
    pub fn new(timeout: Duration) -> Self {
        Self {
            handlers: DisconnectSignal::new(),
            graceful: DisconnectSignal::new(),
            connections: TaskTracker::new(),
            background: TaskTracker::new(),
            timeout,
        }
    }

    /// The signal request handlers observe, released at step 2.
    pub fn signal(&self) -> DisconnectSignal {
        self.handlers.clone()
    }

    /// The signal a connection observes to begin closing, released at step 3.
    pub fn graceful_signal(&self) -> DisconnectSignal {
        self.graceful.clone()
    }

    /// Tracker for connection tasks, awaited at step 3.
    pub fn connections(&self) -> &TaskTracker {
        &self.connections
    }

    /// Tracker for tasks that outlive a single connection, awaited at step 4.
    pub fn tracker(&self) -> &TaskTracker {
        &self.background
    }

    pub fn timeout(&self) -> Duration {
        self.timeout
    }

    /// Run steps 2 through 4. Call this once every accept loop has returned.
    pub async fn drain(self) -> DrainOutcome {
        nitro_observability::metrics::worker_draining();
        self.handlers.trigger();

        let deadline = tokio::time::Instant::from_std(Instant::now() + self.timeout);

        self.graceful.trigger();
        self.connections.close();
        let connections_finished = timeout_at(deadline, self.connections.wait()).await.is_ok();
        if !connections_finished {
            tracing::warn!(
                remaining = self.connections.len(),
                timeout = ?self.timeout,
                "drain timed out with connections still open"
            );
        }

        self.background.close();
        let tasks_finished = timeout_at(deadline, self.background.wait()).await.is_ok();
        if !tasks_finished {
            tracing::warn!(
                remaining = self.background.len(),
                "drain timed out with background tasks still running"
            );
        }

        DrainOutcome {
            connections_finished,
            tasks_finished,
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, Ordering};

    use super::*;

    #[tokio::test]
    async fn handlers_are_released_before_connections_are_closed() {
        let coordinator = DrainCoordinator::new(Duration::from_secs(5));
        let handlers = coordinator.signal();
        let graceful = coordinator.graceful_signal();
        let handlers_first = Arc::new(AtomicBool::new(false));

        coordinator.connections().spawn({
            let handlers = handlers.clone();
            let handlers_first = Arc::clone(&handlers_first);
            async move {
                graceful.wait().await;
                handlers_first.store(handlers.is_triggered(), Ordering::Release);
            }
        });

        let outcome = coordinator.drain().await;
        assert!(outcome.is_complete());
        assert!(
            handlers_first.load(Ordering::Acquire),
            "handlers must be told to wind down before connections are closed"
        );
        assert!(handlers.is_triggered());
    }

    #[tokio::test]
    async fn background_tasks_are_awaited() {
        let coordinator = DrainCoordinator::new(Duration::from_secs(5));
        let finished = Arc::new(AtomicBool::new(false));

        coordinator.tracker().spawn({
            let finished = Arc::clone(&finished);
            async move {
                tokio::time::sleep(Duration::from_millis(20)).await;
                finished.store(true, Ordering::Release);
            }
        });

        let outcome = coordinator.drain().await;
        assert!(outcome.is_complete());
        assert!(finished.load(Ordering::Acquire));
    }

    #[tokio::test]
    async fn a_stuck_connection_does_not_block_shutdown_forever() {
        let coordinator = DrainCoordinator::new(Duration::from_millis(50));
        coordinator
            .connections()
            .spawn(std::future::pending::<()>());

        let outcome = coordinator.drain().await;
        assert!(!outcome.connections_finished);
        assert!(!outcome.is_complete());
    }

    #[tokio::test]
    async fn a_stuck_background_task_does_not_block_shutdown_forever() {
        let coordinator = DrainCoordinator::new(Duration::from_millis(50));
        coordinator.tracker().spawn(std::future::pending::<()>());

        let outcome = coordinator.drain().await;
        assert!(outcome.connections_finished);
        assert!(!outcome.tasks_finished);
    }

    #[tokio::test]
    async fn the_deadline_covers_both_stages_together() {
        let coordinator = DrainCoordinator::new(Duration::from_millis(150));
        coordinator
            .connections()
            .spawn(async { tokio::time::sleep(Duration::from_millis(100)).await });
        coordinator
            .tracker()
            .spawn(async { tokio::time::sleep(Duration::from_secs(30)).await });

        let started = Instant::now();
        let outcome = coordinator.drain().await;

        assert!(outcome.connections_finished);
        assert!(!outcome.tasks_finished);
        assert!(
            started.elapsed() < Duration::from_millis(600),
            "the two stages must share one deadline rather than each getting a full one"
        );
    }

    #[tokio::test]
    async fn draining_with_nothing_open_completes_immediately() {
        let coordinator = DrainCoordinator::new(Duration::from_secs(30));
        let started = Instant::now();
        assert!(coordinator.drain().await.is_complete());
        assert!(started.elapsed() < Duration::from_millis(200));
    }
}
