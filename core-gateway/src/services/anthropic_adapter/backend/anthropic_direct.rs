// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Anthropic Direct Backend
//!
//! Passthrough backend for the Anthropic Messages API. Forwards requests
//! to `api.anthropic.com` (or a custom `api_url`) with minimal transformation.
//! All `anthropic-beta` headers are passed through as-is.
//! Streaming is native Anthropic SSE — pure passthrough with observer tee.

use serde_json::Value;

use super::{BackendAdapter, BetaFilter};
use crate::models::db::LlmModel;

// ---------------------------------------------------------------------------
// AnthropicDirectBackend
// ---------------------------------------------------------------------------

pub struct AnthropicDirectBackend;

impl AnthropicDirectBackend {
    /// Resolve the base URL for an Anthropic model.
    fn base_url(model: &LlmModel) -> String {
        if let Some(ref url) = model.api_url {
            if !url.is_empty() {
                return url.trim_end_matches('/').to_string();
            }
        }
        "https://api.anthropic.com/v1".to_string()
    }
}

impl BackendAdapter for AnthropicDirectBackend {
    fn backend_name(&self) -> &str {
        "anthropic_direct"
    }

    fn messages_url(&self, model: &LlmModel, _streaming: bool) -> String {
        // Anthropic uses the same endpoint for streaming and non-streaming
        // (controlled by `stream` field in the body)
        format!("{}/messages", Self::base_url(model))
    }

    fn count_tokens_url(&self, model: &LlmModel) -> Option<String> {
        Some(format!("{}/messages/count_tokens", Self::base_url(model)))
    }

    fn auth_headers(&self, model: &LlmModel) -> Vec<(String, String)> {
        let mut headers = Vec::new();
        if let Some(ref key) = model.api_key {
            headers.push(("x-api-key".into(), key.clone()));
        }
        headers
    }

    fn filter_beta_headers(&self, beta_value: &str) -> Option<String> {
        // Anthropic Direct: pass ALL beta headers through
        BetaFilter::allow_all().filter(beta_value)
    }

    fn transform_request(&self, _body: &mut Value, _model: &LlmModel) {
        // No-op: Anthropic Direct accepts the native format as-is
    }

    fn native_anthropic_streaming(&self) -> bool {
        true
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn test_model(api_url: Option<&str>) -> LlmModel {
        let mut m = crate::services::llm_adapter::make_test_model(
            "anthropic",
            "claude-sonnet-4-6-20250514",
        );
        m.api_url = api_url.map(String::from);
        m
    }

    #[test]
    fn backend_name() {
        assert_eq!(AnthropicDirectBackend.backend_name(), "anthropic_direct");
    }

    #[test]
    fn messages_url_default() {
        let model = test_model(None);
        assert_eq!(
            AnthropicDirectBackend.messages_url(&model, false),
            "https://api.anthropic.com/v1/messages"
        );
    }

    #[test]
    fn messages_url_custom() {
        let model = test_model(Some("https://custom-proxy.example.com/v1"));
        assert_eq!(
            AnthropicDirectBackend.messages_url(&model, false),
            "https://custom-proxy.example.com/v1/messages"
        );
    }

    #[test]
    fn count_tokens_url() {
        let model = test_model(None);
        assert_eq!(
            AnthropicDirectBackend.count_tokens_url(&model).unwrap(),
            "https://api.anthropic.com/v1/messages/count_tokens"
        );
    }

    #[test]
    fn auth_headers_with_key() {
        let model = test_model(None);
        let headers = AnthropicDirectBackend.auth_headers(&model);
        assert_eq!(headers.len(), 1);
        assert_eq!(headers[0].0, "x-api-key");
    }

    #[test]
    fn beta_filter_passes_all() {
        let result = AnthropicDirectBackend
            .filter_beta_headers("claude-code-20250219,interleaved-thinking-2025-01-24");
        assert_eq!(
            result.unwrap(),
            "claude-code-20250219,interleaved-thinking-2025-01-24"
        );
    }

    #[test]
    fn native_streaming() {
        assert!(AnthropicDirectBackend.native_anthropic_streaming());
    }
}
