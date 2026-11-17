// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Brutor Agent Control Core Gateway — Entry Point
//!
//! The Community gateway: reads the configuration and hands over to
//! `GatewayBuilder` (`src/builder.rs`), which connects the pools, builds the
//! state, mounts the routes, starts the tasks and serves until shutdown. It
//! composes no extension: no licence key, no private
//! dependency. The Enterprise distribution is the binary of `enterprise/`,
//! which composes its extensions into this same builder.
//!
//! ### Key Responsibilities:
//! - **State Management**: Bundles DB and Redis into an `Arc<AppState>` for thread-safe access.
//! - **Middleware Stack**: Implements Layered security (Tenant Isolation, CORS, Tracing).
//! - **Async Runtime**: Leverages `Tokio` for non-blocking I/O across LLM and MCP streams.
//! - **Graceful Shutdown**: Listens for termination signals to drain in-flight AI requests.
//!
//! Using the standard for Rust web services:
//! Axum (web framework), Tokio (async runtime), and Tower (middleware)

#![forbid(unsafe_code)] // No `unsafe`, enforced by the compiler.

use brutor_gateway_core::builder::GatewayBuilder;
use brutor_gateway_core::{config, services};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // The configuration and the telemetry are the environment's. The builder
    // reads it once, first thing in `run`, through the registry of the
    // variables this composition reads (`src/env_vars.rs`): a retired or
    // removed name, a malformed value or a missing required variable stops
    // the gateway there, one line per name and never a value. Started with
    // `--print-env` it prints that registry as JSON and reads no variable.
    let config = config::Source::environment(config::COMMUNITY);
    let telemetry = services::telemetry::Source::Environment;

    // Everything — pools, state, routes, tasks, serving, shutdown — is the
    // builder's (`src/builder.rs`).
    GatewayBuilder::new(config).telemetry(telemetry).run().await
}
