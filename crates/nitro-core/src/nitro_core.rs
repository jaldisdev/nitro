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
pub mod lifecycle;
pub mod router;
pub mod streaming;
pub mod transport;
pub mod websocket;
