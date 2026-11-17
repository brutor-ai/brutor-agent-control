// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Client Adapters
//!
//! Layer 1 of the Anthropic Message Adapter stack. Client adapters handle
//! protocol differences between different developer tools that speak the
//! Anthropic Messages API (Claude Code, Cursor, generic SDK clients, etc.).
//!
//! Detection uses `anthropic-beta` header flags as the primary signal
//! (more reliable than User-Agent), with UA as a secondary fallback.

mod claude_code;
mod generic;

use std::collections::{HashMap, HashSet};

use axum::http::HeaderMap;
use serde_json::Value;

pub use claude_code::ClaudeCodeClient;
pub use generic::GenericClient;

// ---------------------------------------------------------------------------
// ClientMetadata — travels with the request through the pipeline
// ---------------------------------------------------------------------------

/// Metadata extracted from the client's request headers.
/// Passed to backend adapters so they can make informed decisions about
/// header forwarding, body transforms, etc.
#[derive(Debug, Clone)]
pub struct ClientMetadata {
    /// Which client adapter detected this request
    pub client_name: String,
    /// Raw `anthropic-beta` header value (comma-separated flags)
    pub anthropic_beta: Option<String>,
    /// Raw `anthropic-version` header value
    pub anthropic_version: Option<String>,
    /// Any other client-specific headers worth preserving
    pub extra_headers: HashMap<String, String>,
    /// Detected feature flags (e.g. "extended_thinking", "interleaved_thinking")
    pub features: HashSet<String>,
}

// ---------------------------------------------------------------------------
// ClientAdapter trait
// ---------------------------------------------------------------------------

/// Adapter interface for client-specific protocol differences.
///
/// Each implementation handles the quirks of a specific developer tool
/// (Claude Code, Cursor, generic Anthropic SDK, etc.) without coupling
/// to the backend layer.
pub trait ClientAdapter: Send + Sync {
    /// Client identifier (e.g. "claude_code", "generic")
    fn client_name(&self) -> &str;

    /// Extract client-specific headers that need special handling.
    /// Returns `ClientMetadata` that travels with the request through the pipeline.
    fn extract_client_headers(&self, headers: &HeaderMap) -> ClientMetadata;

    /// Pre-process the request body before it hits the backend.
    ///
    /// Receives the `backend_name` so the client adapter can make
    /// backend-aware decisions (e.g. strip `tool_reference` blocks
    /// only for non-Anthropic backends).
    fn preprocess_request(&self, body: &mut Value, metadata: &ClientMetadata, backend_name: &str);

    /// Post-process response before sending back to client.
    /// Default: no-op.
    fn postprocess_response(&self, _body: &mut Value) {}
}

// ---------------------------------------------------------------------------
// Client detection
// ---------------------------------------------------------------------------

/// Detect which client adapter to use based on request headers.
///
/// Uses `anthropic-beta` header content as the **primary signal** (the
/// `claude-code-*` beta flag is always present and directly identifies
/// the client). User-Agent is a **secondary fallback** since UA strings
/// are fragile and change between versions.
pub fn detect_client(headers: &HeaderMap) -> &'static dyn ClientAdapter {
    // Primary: check anthropic-beta header for Claude Code-specific flags
    if let Some(beta) = headers.get("anthropic-beta").and_then(|v| v.to_str().ok()) {
        if beta.contains("claude-code-") {
            return &ClaudeCodeClient;
        }
    }

    // Secondary: User-Agent fallback
    let ua = headers
        .get("user-agent")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if ua.contains("claude-code") || ua.contains("ClaudeCode") || ua.contains("anthropic-cli") {
        return &ClaudeCodeClient;
    }

    // Future: Cursor, Windsurf, Cline, aider, etc.
    // if beta.contains("cursor-") || ua.contains("Cursor") { return &CursorClient; }

    &GenericClient
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn headers_with(pairs: &[(&str, &str)]) -> HeaderMap {
        let mut map = HeaderMap::new();
        for (k, v) in pairs {
            map.insert(
                axum::http::HeaderName::from_bytes(k.as_bytes()).unwrap(),
                axum::http::HeaderValue::from_str(v).unwrap(),
            );
        }
        map
    }

    #[test]
    fn detect_claude_code_by_beta_header() {
        let headers = headers_with(&[(
            "anthropic-beta",
            "claude-code-20250219,interleaved-thinking-2025-01-24",
        )]);
        let client = detect_client(&headers);
        assert_eq!(client.client_name(), "claude_code");
    }

    #[test]
    fn detect_claude_code_by_user_agent() {
        let headers = headers_with(&[("user-agent", "claude-code/2.1.69")]);
        let client = detect_client(&headers);
        assert_eq!(client.client_name(), "claude_code");
    }

    #[test]
    fn detect_generic_for_plain_sdk() {
        let headers = headers_with(&[("user-agent", "python-anthropic/0.28.0")]);
        let client = detect_client(&headers);
        assert_eq!(client.client_name(), "generic");
    }

    #[test]
    fn detect_generic_for_empty_headers() {
        let headers = HeaderMap::new();
        let client = detect_client(&headers);
        assert_eq!(client.client_name(), "generic");
    }

    #[test]
    fn beta_header_primary_over_ua() {
        // Beta header says claude-code, UA says something else
        let headers = headers_with(&[
            ("anthropic-beta", "claude-code-20250219"),
            ("user-agent", "some-other-tool/1.0"),
        ]);
        let client = detect_client(&headers);
        assert_eq!(client.client_name(), "claude_code");
    }
}
