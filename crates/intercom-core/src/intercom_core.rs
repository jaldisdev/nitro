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
