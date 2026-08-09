//! Publish/subscribe channels for real-time transports.
//!
//! The crate is deliberately free of any Python awareness so that other Rust
//! services can depend on it without pulling in an interpreter.

pub mod channel;
pub mod codec;
pub mod redis;

pub use channel::{ChannelConfig, unique_channel};
pub use codec::{CodecError, Value};
pub use redis::{ChannelReader, Intercom, IntercomError, Subscription};
