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

//! The Redis backend.
//!
//! Two ways of moving a message are offered, because they answer different
//! questions.
//!
//! `publish`/`subscribe` is push delivery: a subscriber that is connected gets
//! the message immediately, and one that is not never learns of it. That is the
//! right shape for a live socket, where a client that has gone away has nothing
//! to catch up on.
//!
//! `send`/`receive` is a bounded queue: a message waits until it is read or
//! until the channel expires, and the oldest is discarded once the channel is
//! full. That is the right shape when a reader may briefly be elsewhere and
//! should not miss what happened while it was.
//!
//! Groups work with either, and only ever hold channel names.

use bytes::Bytes;
use futures_util::StreamExt;
use redis::AsyncCommands;
use redis::aio::{ConnectionManager, PubSub};

use crate::channel::ChannelConfig;

#[derive(Debug, thiserror::Error)]
pub enum IntercomError {
    #[error("could not reach the message store: {0}")]
    Connection(String),
    #[error("the message store rejected the operation: {0}")]
    Command(String),
}

fn command(error: redis::RedisError) -> IntercomError {
    if error.is_connection_dropped() || error.is_io_error() {
        IntercomError::Connection(error.to_string())
    } else {
        IntercomError::Command(error.to_string())
    }
}

/// A connection to the message store.
///
/// Cloning is cheap and shares one multiplexed connection, which reconnects on
/// its own if the server goes away — so a caller holding a clone across a
/// restart keeps working rather than having to notice and rebuild.
#[derive(Clone)]
pub struct Intercom {
    client: redis::Client,
    connection: ConnectionManager,
    config: ChannelConfig,
}

impl std::fmt::Debug for Intercom {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Intercom")
            .field("config", &self.config)
            .finish_non_exhaustive()
    }
}

impl Intercom {
    pub async fn connect(url: &str, config: ChannelConfig) -> Result<Self, IntercomError> {
        let client = redis::Client::open(url).map_err(|error| {
            IntercomError::Connection(format!("{url:?} is not a usable address: {error}"))
        })?;
        let connection = ConnectionManager::new(client.clone())
            .await
            .map_err(|error| IntercomError::Connection(error.to_string()))?;

        Ok(Self {
            client,
            connection,
            config,
        })
    }

    pub fn config(&self) -> &ChannelConfig {
        &self.config
    }

    // ── push delivery ────────────────────────────────────────────────────────

    /// Deliver to whoever is subscribed right now, and report how many that was.
    pub async fn publish(&self, channel: &str, payload: Bytes) -> Result<u64, IntercomError> {
        let mut connection = self.connection.clone();
        connection
            .publish(self.config.channel_key(channel), payload.as_ref())
            .await
            .map_err(command)
    }

    /// Listen to a channel. The subscription owns a connection of its own,
    /// because a subscribed connection cannot be used for anything else.
    pub async fn subscribe(&self, channel: &str) -> Result<Subscription, IntercomError> {
        let mut pubsub = self
            .client
            .get_async_pubsub()
            .await
            .map_err(|error| IntercomError::Connection(error.to_string()))?;
        pubsub
            .subscribe(self.config.channel_key(channel))
            .await
            .map_err(command)?;

        Ok(Subscription { pubsub })
    }

    /// Publish to every channel in a group, reporting how many subscribers were
    /// reached in total.
    pub async fn group_publish(&self, group: &str, payload: Bytes) -> Result<u64, IntercomError> {
        let mut reached = 0;
        for channel in self.group_channels(group).await? {
            reached += self.publish(&channel, payload.clone()).await?;
        }
        Ok(reached)
    }

    // ── queued delivery ──────────────────────────────────────────────────────

    /// Queue a message for whoever reads the channel next.
    pub async fn send(&self, channel: &str, payload: Bytes) -> Result<(), IntercomError> {
        let key = self.config.channel_key(channel);
        let mut connection = self.connection.clone();

        // One round trip, and the trim and expiry cannot be skipped by a
        // failure in between: an untrimmed channel would grow without bound and
        // an unexpiring one would outlive its reader.
        redis::pipe()
            .atomic()
            .lpush(&key, payload.as_ref())
            .ignore()
            .ltrim(&key, 0, self.config.capacity as isize - 1)
            .ignore()
            .expire(&key, self.config.expiry_seconds() as i64)
            .ignore()
            .exec_async(&mut connection)
            .await
            .map_err(command)
    }

    /// Take the oldest queued message, if there is one.
    pub async fn receive(&self, channel: &str) -> Result<Option<Bytes>, IntercomError> {
        let mut connection = self.connection.clone();
        let payload: Option<Vec<u8>> = connection
            .rpop(self.config.channel_key(channel), None)
            .await
            .map_err(command)?;
        Ok(payload.map(Bytes::from))
    }

    /// A reader dedicated to one channel, able to wait for a message.
    ///
    /// It gets a connection of its own rather than sharing the pooled one. A
    /// blocking read occupies its connection for as long as it waits, and the
    /// pooled connection is multiplexed — waiting on it would stall every other
    /// caller until a message happened to arrive.
    pub async fn reader(&self, channel: &str) -> Result<ChannelReader, IntercomError> {
        // The default response timeout would cut a blocking read short; this
        // connection is only ever used for reads that are meant to wait.
        let settings = redis::AsyncConnectionConfig::new().set_response_timeout(None);
        let connection = self
            .client
            .get_multiplexed_async_connection_with_config(&settings)
            .await
            .map_err(|error| IntercomError::Connection(error.to_string()))?;

        Ok(ChannelReader {
            connection,
            key: self.config.channel_key(channel),
        })
    }

    /// Queue a message for every channel in a group.
    pub async fn group_send(&self, group: &str, payload: Bytes) -> Result<(), IntercomError> {
        let channels = self.group_channels(group).await?;
        if channels.is_empty() {
            return Ok(());
        }

        let mut pipeline = redis::pipe();
        pipeline.atomic();
        for channel in &channels {
            let key = self.config.channel_key(channel);
            pipeline
                .lpush(&key, payload.as_ref())
                .ignore()
                .ltrim(&key, 0, self.config.capacity as isize - 1)
                .ignore()
                .expire(&key, self.config.expiry_seconds() as i64)
                .ignore();
        }

        let mut connection = self.connection.clone();
        pipeline.exec_async(&mut connection).await.map_err(command)
    }

    // ── groups ───────────────────────────────────────────────────────────────

    /// Add a channel to a group. Adding one that is already there changes
    /// nothing except the group's expiry, which is refreshed.
    pub async fn group_add(&self, group: &str, channel: &str) -> Result<(), IntercomError> {
        let key = self.config.group_key(group);
        let mut connection = self.connection.clone();

        redis::pipe()
            .atomic()
            .sadd(&key, channel)
            .ignore()
            .expire(&key, self.config.expiry_seconds() as i64)
            .ignore()
            .exec_async(&mut connection)
            .await
            .map_err(command)
    }

    /// Remove a channel from a group, reporting whether it had been in it.
    pub async fn group_discard(&self, group: &str, channel: &str) -> Result<bool, IntercomError> {
        let mut connection = self.connection.clone();
        let removed: i64 = connection
            .srem(self.config.group_key(group), channel)
            .await
            .map_err(command)?;
        Ok(removed > 0)
    }

    /// Every channel in a group.
    pub async fn group_channels(&self, group: &str) -> Result<Vec<String>, IntercomError> {
        let mut connection = self.connection.clone();
        connection
            .smembers(self.config.group_key(group))
            .await
            .map_err(command)
    }

    pub async fn group_size(&self, group: &str) -> Result<usize, IntercomError> {
        let mut connection = self.connection.clone();
        connection
            .scard(self.config.group_key(group))
            .await
            .map_err(command)
    }

    // ── housekeeping ─────────────────────────────────────────────────────────

    /// Remove every channel and group this configuration owns.
    ///
    /// Keys are found by scanning rather than by `KEYS`, so a large store is
    /// not blocked while this runs, and the prefix is respected so nothing
    /// belonging to another application is touched.
    pub async fn flush(&self) -> Result<usize, IntercomError> {
        let mut connection = self.connection.clone();
        let mut cursor: u64 = 0;
        let mut removed = 0;

        loop {
            let (next, keys): (u64, Vec<String>) = redis::cmd("SCAN")
                .arg(cursor)
                .arg("MATCH")
                .arg(self.config.owned_pattern())
                .arg("COUNT")
                .arg(100)
                .query_async(&mut connection)
                .await
                .map_err(command)?;

            if !keys.is_empty() {
                removed += keys.len();
                let _deleted: i64 = connection.del(keys).await.map_err(command)?;
            }

            cursor = next;
            if cursor == 0 {
                return Ok(removed);
            }
        }
    }

    /// Whether the store is reachable.
    pub async fn ping(&self) -> Result<(), IntercomError> {
        let mut connection = self.connection.clone();
        redis::cmd("PING")
            .query_async::<String>(&mut connection)
            .await
            .map(|_| ())
            .map_err(command)
    }
}

/// A channel reader with a connection of its own.
pub struct ChannelReader {
    connection: redis::aio::MultiplexedConnection,
    key: String,
}

impl std::fmt::Debug for ChannelReader {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ChannelReader")
            .field("key", &self.key)
            .finish_non_exhaustive()
    }
}

impl ChannelReader {
    /// The oldest queued message, waiting up to `timeout` for one to arrive.
    ///
    /// A timeout of zero waits indefinitely, which is what the store means by
    /// zero and what a reader with nothing else to do usually wants.
    pub async fn next_message(
        &mut self,
        timeout: std::time::Duration,
    ) -> Result<Option<Bytes>, IntercomError> {
        let popped: Option<(String, Vec<u8>)> = self
            .connection
            .brpop(&self.key, timeout.as_secs_f64())
            .await
            .map_err(command)?;
        Ok(popped.map(|(_key, payload)| Bytes::from(payload)))
    }

    /// The oldest queued message if one is already waiting.
    pub async fn try_next_message(&mut self) -> Result<Option<Bytes>, IntercomError> {
        let payload: Option<Vec<u8>> = self
            .connection
            .rpop(&self.key, None)
            .await
            .map_err(command)?;
        Ok(payload.map(Bytes::from))
    }
}

/// A live subscription to one channel.
pub struct Subscription {
    pubsub: PubSub,
}

impl std::fmt::Debug for Subscription {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("Subscription").finish()
    }
}

impl Subscription {
    /// The next message, or `None` once the subscription has ended.
    pub async fn next_message(&mut self) -> Option<Bytes> {
        let message = self.pubsub.on_message().next().await?;
        match message.get_payload::<Vec<u8>>() {
            Ok(payload) => Some(Bytes::from(payload)),
            Err(error) => {
                tracing::warn!(%error, "dropping a message whose payload could not be read");
                None
            }
        }
    }

    /// Stop listening.
    pub async fn close(mut self, channel_key: &str) {
        if let Err(error) = self.pubsub.unsubscribe(channel_key).await {
            tracing::debug!(%error, "unsubscribing failed; the connection is closing anyway");
        }
    }
}
