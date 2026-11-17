// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Backend Adapters
//!
//! Layer 3 of the Anthropic Message Adapter stack. Backend adapters handle
//! provider differences between Anthropic Direct, AWS Bedrock, and Google
//! Vertex AI — each with different auth, URL schemes, header requirements,
//! and streaming formats.

mod anthropic_direct;
pub mod bedrock;
pub mod vertex;

use serde_json::Value;

use crate::models::db::LlmModel;

pub use anthropic_direct::AnthropicDirectBackend;
pub use bedrock::{BedrockBackend, BedrockEventStreamConverter};
pub use vertex::VertexBackend;

// ---------------------------------------------------------------------------
// BetaFilter — data-driven header filtering
// ---------------------------------------------------------------------------

/// Configurable filter for `anthropic-beta` header flags.
///
/// Rather than hardcoding which flags to strip, each backend provides a
/// `BetaFilter` that can be updated as backends add support for new flags.
/// When Claude Code ships a new beta flag, update the allowlist — not adapter code.
#[derive(Debug, Clone)]
pub struct BetaFilter {
    /// If true, pass all beta flags through (Anthropic Direct).
    pub allow_all: bool,
    /// Explicit allowlist of supported beta flag prefixes.
    pub allowed_prefixes: Vec<String>,
}

impl BetaFilter {
    /// Allow all beta flags (Anthropic Direct API).
    pub fn allow_all() -> Self {
        Self {
            allow_all: true,
            allowed_prefixes: vec![],
        }
    }

    /// Strip all beta flags (Bedrock, Vertex — reject unknown flags).
    pub fn strip_all() -> Self {
        Self {
            allow_all: false,
            allowed_prefixes: vec![],
        }
    }

    /// Restrictive filter with an explicit allowlist of supported prefixes.
    pub fn with_allowlist(prefixes: Vec<String>) -> Self {
        Self {
            allow_all: false,
            allowed_prefixes: prefixes,
        }
    }

    /// Filter a comma-separated beta header value.
    /// Returns the filtered string, or `None` to omit the header entirely.
    pub fn filter(&self, beta_value: &str) -> Option<String> {
        if self.allow_all {
            return Some(beta_value.to_string());
        }
        if self.allowed_prefixes.is_empty() {
            return None;
        }
        let allowed: Vec<&str> = beta_value
            .split(',')
            .map(|s| s.trim())
            .filter(|flag| {
                self.allowed_prefixes
                    .iter()
                    .any(|prefix| flag.starts_with(prefix.as_str()))
            })
            .collect();
        if allowed.is_empty() {
            None
        } else {
            Some(allowed.join(", "))
        }
    }
}

// ---------------------------------------------------------------------------
// StreamConverter trait
// ---------------------------------------------------------------------------

/// Converts non-Anthropic streaming formats to Anthropic SSE events.
///
/// Used by backends like Bedrock (AWS EventStream binary protocol) that
/// don't stream in native Anthropic SSE format.
pub trait StreamConverter: Send {
    /// Convert a chunk of backend-specific streaming data into zero or more
    /// Anthropic SSE event strings. Each returned string is a complete SSE
    /// `data: <json>` payload (without the `data: ` prefix).
    ///
    /// Return an empty vec if the chunk is incomplete and needs more data.
    fn convert(&mut self, chunk: &[u8]) -> Vec<(String, String)>;
}

// ---------------------------------------------------------------------------
// BackendAdapter trait
// ---------------------------------------------------------------------------

/// Adapter interface for Anthropic Messages API backend providers.
///
/// Each implementation handles the specifics of routing native Anthropic
/// Messages format to a particular backend (Anthropic Direct, Bedrock, Vertex).
pub trait BackendAdapter: Send + Sync {
    /// Backend identifier (e.g. "anthropic_direct", "bedrock", "vertex")
    fn backend_name(&self) -> &str;

    /// Build the upstream URL for `/v1/messages`.
    /// `streaming` indicates whether the client wants SSE streaming.
    fn messages_url(&self, model: &LlmModel, streaming: bool) -> String;

    /// Build the upstream URL for `/v1/messages/count_tokens`.
    /// Returns `None` if this backend doesn't support token counting natively.
    fn count_tokens_url(&self, model: &LlmModel) -> Option<String>;

    /// Build auth and provider-specific headers for this backend.
    fn auth_headers(&self, model: &LlmModel) -> Vec<(String, String)>;

    /// Filter `anthropic-beta` flags for this backend.
    /// Returns the filtered comma-separated string, or `None` to omit the header.
    fn filter_beta_headers(&self, beta_value: &str) -> Option<String>;

    /// Transform the Anthropic Messages request body for this backend.
    /// Called after client preprocessing.
    fn transform_request(&self, body: &mut Value, model: &LlmModel);

    /// Whether streaming responses from this backend are in native Anthropic
    /// SSE format. If `true`, events are passed through to the client as-is.
    /// If `false`, `create_stream_converter()` must provide a converter.
    fn native_anthropic_streaming(&self) -> bool {
        true
    }

    /// Create a stream converter for non-native streaming formats.
    /// Only called when `native_anthropic_streaming()` returns `false`.
    fn create_stream_converter(&self) -> Option<Box<dyn StreamConverter>> {
        None
    }
}

// ---------------------------------------------------------------------------
// Backend lookup
// ---------------------------------------------------------------------------

/// Look up the backend adapter for a provider name.
///
/// Only returns backends that speak the Anthropic Messages format natively.
/// For other providers, the caller should fall back to the existing
/// Anthropic → OpenAI translation pipeline.
pub fn get_backend(provider: &str) -> Option<&'static dyn BackendAdapter> {
    match provider {
        "anthropic" => Some(&AnthropicDirectBackend),
        "bedrock" | "bedrock_converse" => Some(&BedrockBackend),
        "vertex_ai" | "vertex" => Some(&VertexBackend),
        _ => None,
    }
}

/// Check if a provider has a native Anthropic Messages backend: exactly the
/// providers [`get_backend`] answers for. `vertex` is `vertex_ai`
/// (the chat route sent a Claude row of `vertex` to the native
/// bridge, which had no backend for that name), and `bedrock_converse` is
/// Bedrock (the Messages route sent a Claude row of it to the OpenAI
/// translation and the API-key forwarder).
pub fn is_native_anthropic_provider(provider: &str) -> bool {
    get_backend(provider).is_some()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // -- BetaFilter tests --

    #[test]
    fn beta_filter_allow_all_passes_everything() {
        let filter = BetaFilter::allow_all();
        let result = filter.filter(
            "claude-code-20250219,interleaved-thinking-2025-01-24,extended-thinking-2025-01-24",
        );
        assert_eq!(
            result.unwrap(),
            "claude-code-20250219,interleaved-thinking-2025-01-24,extended-thinking-2025-01-24"
        );
    }

    #[test]
    fn beta_filter_strip_all_returns_none() {
        let filter = BetaFilter::strip_all();
        let result = filter.filter("claude-code-20250219,interleaved-thinking-2025-01-24");
        assert!(result.is_none());
    }

    #[test]
    fn beta_filter_allowlist_filters_correctly() {
        let filter = BetaFilter::with_allowlist(vec![
            "interleaved-thinking".into(),
            "extended-thinking".into(),
        ]);
        let result = filter.filter(
            "claude-code-20250219, interleaved-thinking-2025-01-24, advanced-tool-use-2025-11-20, extended-thinking-2025-01-24",
        );
        assert_eq!(
            result.unwrap(),
            "interleaved-thinking-2025-01-24, extended-thinking-2025-01-24"
        );
    }

    #[test]
    fn beta_filter_allowlist_returns_none_when_no_match() {
        let filter = BetaFilter::with_allowlist(vec!["nonexistent-feature".into()]);
        let result = filter.filter("claude-code-20250219,interleaved-thinking-2025-01-24");
        assert!(result.is_none());
    }

    // -- Backend lookup tests --

    #[test]
    fn get_backend_anthropic() {
        let b = get_backend("anthropic").unwrap();
        assert_eq!(b.backend_name(), "anthropic_direct");
    }

    #[test]
    fn get_backend_bedrock() {
        let b = get_backend("bedrock").unwrap();
        assert_eq!(b.backend_name(), "bedrock");
    }

    #[test]
    fn get_backend_vertex() {
        let b = get_backend("vertex_ai").unwrap();
        assert_eq!(b.backend_name(), "vertex");
    }

    #[test]
    fn get_backend_unknown_returns_none() {
        assert!(get_backend("openai").is_none());
        assert!(get_backend("gemini").is_none());
    }

    #[test]
    fn is_native_anthropic_provider_checks() {
        assert!(is_native_anthropic_provider("anthropic"));
        assert!(is_native_anthropic_provider("bedrock"));
        assert!(is_native_anthropic_provider("vertex_ai"));
        assert!(is_native_anthropic_provider("vertex"));
        assert!(is_native_anthropic_provider("bedrock_converse"));
        assert_eq!(
            get_backend("vertex").map(|b| b.backend_name()),
            get_backend("vertex_ai").map(|b| b.backend_name())
        );
        assert!(!is_native_anthropic_provider("openai"));
        assert!(!is_native_anthropic_provider("gemini"));
    }
}
