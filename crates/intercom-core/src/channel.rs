//! Channels and groups.
//!
//! A channel is a plain string. There is no registry, no schema and no
//! discovery: two parties agree on a name by convention and test that agreement
//! before they deploy. Anything more would be a second source of truth about
//! names that are already written down in the code on both sides.
//!
//! A group is a named set of channels, so a message can be addressed to
//! "everyone in room 4" without the sender knowing who that is.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// How channels and groups are named in the backing store.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChannelConfig {
    /// Prepended to every key, so several applications can share one server.
    pub prefix: String,
    /// How many messages a channel holds before the oldest is discarded.
    pub capacity: usize,
    /// How long an idle channel or group survives. Without this, a client that
    /// vanishes without saying goodbye would leave its channel behind forever.
    pub expiry: Duration,
}

impl Default for ChannelConfig {
    fn default() -> Self {
        Self {
            prefix: String::new(),
            capacity: 100,
            expiry: Duration::from_secs(60),
        }
    }
}

impl ChannelConfig {
    pub fn with_prefix(prefix: impl Into<String>) -> Self {
        Self {
            prefix: prefix.into(),
            ..Self::default()
        }
    }

    pub fn channel_key(&self, channel: &str) -> String {
        self.key(&format!("channel:{channel}"))
    }

    pub fn group_key(&self, group: &str) -> String {
        self.key(&format!("group:{group}"))
    }

    /// A pattern matching every key this configuration owns.
    pub fn owned_pattern(&self) -> String {
        self.key("*")
    }

    /// Expiry in whole seconds, which is the granularity the store works in.
    /// Anything shorter than a second becomes one rather than zero, since zero
    /// would mean "no expiry" and keep the key forever.
    pub fn expiry_seconds(&self) -> u64 {
        self.expiry.as_secs().max(1)
    }

    fn key(&self, suffix: &str) -> String {
        if self.prefix.is_empty() {
            suffix.to_owned()
        } else {
            format!("{}:{suffix}", self.prefix)
        }
    }
}

/// Counter making names unique within a process, alongside the process id and
/// the clock which separate processes from one another.
static SEQUENCE: AtomicU64 = AtomicU64::new(0);

/// A channel name nothing else is using.
///
/// Used for the per-connection channel a socket listens on, where the name only
/// has to be unique rather than meaningful.
pub fn unique_channel(prefix: &str) -> String {
    let sequence = SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let moment = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or(0);

    format!("{prefix}.{:x}.{moment:x}.{sequence:x}", std::process::id())
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;

    use super::*;

    #[test]
    fn keys_are_namespaced_by_kind() {
        let config = ChannelConfig::default();
        assert_eq!(config.channel_key("room"), "channel:room");
        assert_eq!(config.group_key("room"), "group:room");
    }

    #[test]
    fn a_prefix_is_applied_to_every_key() {
        let config = ChannelConfig::with_prefix("app");
        assert_eq!(config.channel_key("room"), "app:channel:room");
        assert_eq!(config.group_key("room"), "app:group:room");
        assert_eq!(config.owned_pattern(), "app:*");
    }

    #[test]
    fn a_channel_and_a_group_of_the_same_name_do_not_collide() {
        let config = ChannelConfig::with_prefix("app");
        assert_ne!(config.channel_key("chat"), config.group_key("chat"));
    }

    #[test]
    fn expiry_is_never_rounded_down_to_nothing() {
        let config = ChannelConfig {
            expiry: Duration::from_millis(1),
            ..Default::default()
        };
        assert_eq!(
            config.expiry_seconds(),
            1,
            "an expiry of zero would mean the key never expires"
        );
    }

    #[test]
    fn expiry_is_reported_in_seconds() {
        let config = ChannelConfig {
            expiry: Duration::from_secs(90),
            ..Default::default()
        };
        assert_eq!(config.expiry_seconds(), 90);
    }

    #[test]
    fn unique_names_carry_their_prefix() {
        assert!(unique_channel("socket").starts_with("socket."));
    }

    #[test]
    fn unique_names_do_not_repeat() {
        let names: HashSet<String> = (0..1000).map(|_| unique_channel("socket")).collect();
        assert_eq!(names.len(), 1000);
    }
}
