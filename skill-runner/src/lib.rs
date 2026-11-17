// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Brutor Skill Runner — library entry point.
//!
//! Re-exports the public types and routes for testing. The binary
//! crate (`main.rs`) is a thin orchestrator that loads config, builds
//! the axum app via [`api::build_router`], and runs it.
//!
//! # Architecture (one page)
//!
//! The runner is a stateless HTTP service. The proxy sends a
//! self-contained execution payload — scripts + assets + resources
//! inline — and the runner returns the captured output. The runner
//! NEVER reads from Postgres, NEVER holds proxy secrets, NEVER calls
//! upstream LLM/MCP providers. The only egress it ever needs is the
//! HTTP response itself.
//!
//! Threat model and hardening rationale are in
//! `THREAT-MODEL.md` at the repository root.

pub mod api;
pub mod auth;
pub mod config;
pub mod env_registry;
pub mod env_vars;
pub mod executor;
pub mod logging;
pub mod metrics;
pub mod types;
pub mod workspace;
