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

//! Transport, lifecycle and routing core for the Nitro server.
//!
//! This crate knows nothing about Python. Everything that needs to call into an
//! embedded interpreter is expressed through the [`Dispatch`] trait, which the
//! binding crate implements. That keeps the whole request path unit-testable
//! without an interpreter present.

pub mod config;
pub mod disconnect;
pub mod files;
pub mod headers;
pub mod hosts;
pub mod lifecycle;
/// The metrics crate, re-exported so binding code has one path to every type
/// the server is configured with.
pub use nitro_observability as observability;

pub mod router;
pub mod streaming;
pub mod transport;
pub mod websocket;
pub mod webtransport;
