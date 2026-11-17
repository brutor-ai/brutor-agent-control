// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Generalized call-chain primitive.
//!
//! The HMAC-signed delegation chain that started life as
//! `services::a2a_delegation` is the gateway's canonical primitive
//! for tracking a single user prompt across nested LLM, MCP, A2A,
//! and Skill calls. This module is the **public name** for that
//! primitive — every surface other than A2A reaches for the
//! `call_chain` namespace; A2A keeps `a2a_delegation` for back-
//! compat with the existing routes and tests.
//!
//! The two paths re-export the same items:
//!
//!   * `DelegationChain` — the underlying struct (name kept for
//!     back-compat with A2A peers that produce delegation headers
//!     today).
//!   * `CallChain` — the cross-surface alias every new caller
//!     should use; `pub type CallChain = DelegationChain` so the
//!     two are the same data shape at the type level.
//!   * `parse_or_mint`, `derive_tenant_key`, the four `HEADER_*`
//!     constants, `ABSOLUTE_MAX_DEPTH`, `TraceContinuity`,
//!     `DelegationError` — everything else the route handlers and
//!     the proxy logger need.
//!
//! Why this is a separate module rather than just renaming the
//! file: existing A2A code (`routes::a2a`,
//! `tests::a2a_outbound_integration`) uses `crate::services::
//! a2a_delegation::...` paths that ship in the trial bundle and
//! are referenced from policy-as-code exports / Mission Control
//! aggregations. Renaming would force a coordinated cross-repo
//! update. Re-exporting under the new name gives the canonical
//! `call_chain` path immediately while leaving `a2a_delegation`
//! valid as an alias.

pub use crate::services::a2a_delegation::{
    ABSOLUTE_MAX_DEPTH, CallChain, DelegationChain, DelegationError, HEADER_DEPTH, HEADER_PARENT,
    HEADER_ROOT, HEADER_RUN_ID, HEADER_SIG, MAX_RUN_ID_LEN, TraceContinuity, asserted_root,
    derive_asserted_root, derive_tenant_key, parse_or_mint,
};

// ---------------------------------------------------------------------------
// Outbound header propagation
// ---------------------------------------------------------------------------
//
// The LLM router's upstream HTTP send sits ~12 layers deep below
// the route handler that owns the call chain (`forward_with_fallbacks`
// → `try_fallback_list` → `forward_chat_completion_with_prompt_cache`
// → `forward_chat_completion` → `forward_chat_completion_impl` → the
// actual `client.post(...)` builder). Threading a chain parameter
// through every signature would cascade across LLM + Anthropic +
// Gemini + Bedrock routes and the streaming variants — a 30+ file
// surface change for what is really one per-request piece of state.
//
// `tokio::task_local!` solves this: the route handler sets the
// header pairs once at request entry; the deep send-site reads
// them when constructing the `reqwest::RequestBuilder`. Tokio
// guarantees the local is isolated per Future, so concurrent
// requests cannot read each other's chain headers.
//
// The MCP route does its outbound send at depth 0 (route handler
// constructs the request directly), so it doesn't need the
// task-local — it writes the headers inline. Future LLM/Skill
// chain-propagation work can re-use the same task-local.

tokio::task_local! {
    /// Outbound HTTP chain headers for the current request. Set
    /// at route entry by callers that own a verified
    /// [`CallChain`]; read by the upstream-send sites
    /// (currently `services::llm_router::forward_chat_completion_impl`).
    ///
    /// Stored as already-rendered (header-name, value) pairs to
    /// keep the read-side cheap — every reqwest builder iterates
    /// these and emits `.header(name, value)`.
    pub static OUTBOUND_CHAIN_HEADERS: Vec<(&'static str, String)>;
}

/// Try to read the current task's outbound chain headers. Returns
/// `None` when the task-local hasn't been set (e.g. tests, MCP
/// route which doesn't need it, or any call-site upstream of a
/// route handler that hasn't yet wired Feature 1).
pub fn current_outbound_headers() -> Option<Vec<(&'static str, String)>> {
    OUTBOUND_CHAIN_HEADERS.try_with(|h| h.clone()).ok()
}
