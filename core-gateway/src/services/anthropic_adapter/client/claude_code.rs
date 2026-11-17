// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Claude Code Client Adapter
//!
//! Handles protocol quirks specific to Claude Code:
//! - Extracts and preserves `anthropic-beta` feature flags
//! - Detects active features (extended thinking, interleaved thinking, etc.)
//! - Strips `tool_reference` blocks for backends that don't support them
//! - Preserves `effort` and `thinking` parameters for backend mapping

use axum::http::HeaderMap;
use serde_json::Value;
use std::collections::{HashMap, HashSet};

use super::{ClientAdapter, ClientMetadata};

// ---------------------------------------------------------------------------
// Known Claude Code beta flag prefixes for feature detection
// ---------------------------------------------------------------------------

const FEATURE_EXTENDED_THINKING: &str = "extended-thinking";
const FEATURE_INTERLEAVED_THINKING: &str = "interleaved-thinking";
const FEATURE_TOOL_USE: &str = "tool-use";
const FEATURE_ADVANCED_TOOL_USE: &str = "advanced-tool-use";
const FEATURE_STRUCTURED_OUTPUTS: &str = "structured-outputs";
const FEATURE_EFFORT: &str = "effort";

/// Map beta flag prefixes to feature names for detection.
const BETA_FEATURE_MAP: &[(&str, &str)] = &[
    ("extended-thinking", FEATURE_EXTENDED_THINKING),
    ("interleaved-thinking", FEATURE_INTERLEAVED_THINKING),
    ("tool-use", FEATURE_TOOL_USE),
    ("advanced-tool-use", FEATURE_ADVANCED_TOOL_USE),
    ("structured-outputs", FEATURE_STRUCTURED_OUTPUTS),
    ("effort", FEATURE_EFFORT),
];

// ---------------------------------------------------------------------------
// ClaudeCodeClient
// ---------------------------------------------------------------------------

pub struct ClaudeCodeClient;

impl ClaudeCodeClient {
    /// Parse the anthropic-beta header into individual flags and detect features.
    fn detect_features(beta_value: &str) -> HashSet<String> {
        let mut features = HashSet::new();
        for flag in beta_value.split(',').map(|s| s.trim()) {
            for &(prefix, feature_name) in BETA_FEATURE_MAP {
                if flag.starts_with(prefix) {
                    features.insert(feature_name.to_string());
                }
            }
        }
        features
    }

    /// Strip `tool_reference` content blocks from messages.
    /// Claude Code sends these for tool search, but non-Anthropic backends reject them.
    fn strip_tool_references(body: &mut Value) {
        if let Some(messages) = body.get_mut("messages").and_then(|m| m.as_array_mut()) {
            for msg in messages.iter_mut() {
                if let Some(content) = msg.get_mut("content").and_then(|c| c.as_array_mut()) {
                    content.retain(|block| {
                        block
                            .get("type")
                            .and_then(|t| t.as_str())
                            .map_or(true, |t| t != "tool_reference")
                    });
                }
            }
        }
    }
}

impl ClientAdapter for ClaudeCodeClient {
    fn client_name(&self) -> &str {
        "claude_code"
    }

    fn extract_client_headers(&self, headers: &HeaderMap) -> ClientMetadata {
        let anthropic_beta = headers
            .get("anthropic-beta")
            .and_then(|v| v.to_str().ok())
            .map(String::from);

        let anthropic_version = headers
            .get("anthropic-version")
            .and_then(|v| v.to_str().ok())
            .map(String::from);

        let features = anthropic_beta
            .as_deref()
            .map(Self::detect_features)
            .unwrap_or_default();

        ClientMetadata {
            client_name: "claude_code".into(),
            anthropic_beta,
            anthropic_version,
            extra_headers: HashMap::new(),
            features,
        }
    }

    fn preprocess_request(&self, body: &mut Value, _metadata: &ClientMetadata, backend_name: &str) {
        // Strip tool_reference blocks for non-Anthropic-Direct backends
        // (Bedrock and Vertex reject unknown content block types)
        if backend_name != "anthropic_direct" {
            Self::strip_tool_references(body);
        }

        // Note: `effort` and `thinking` parameters are preserved in the body.
        // Backend adapters handle the mapping (e.g. effort → thinking.budget_tokens
        // for Bedrock).
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn client_name() {
        assert_eq!(ClaudeCodeClient.client_name(), "claude_code");
    }

    #[test]
    fn detect_features_from_beta_header() {
        let features = ClaudeCodeClient::detect_features(
            "claude-code-20250219,interleaved-thinking-2025-01-24,extended-thinking-2025-01-24,advanced-tool-use-2025-11-20",
        );
        assert!(
            features.contains("interleaved_thinking") || features.contains("interleaved-thinking")
        );
        assert!(features.contains("extended_thinking") || features.contains("extended-thinking"));
        assert!(features.contains("advanced_tool_use") || features.contains("advanced-tool-use"));
    }

    #[test]
    fn extract_headers_captures_beta_and_version() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "anthropic-beta",
            "claude-code-20250219,interleaved-thinking-2025-01-24"
                .parse()
                .unwrap(),
        );
        headers.insert("anthropic-version", "2023-06-01".parse().unwrap());

        let metadata = ClaudeCodeClient.extract_client_headers(&headers);
        assert_eq!(metadata.client_name, "claude_code");
        assert!(
            metadata
                .anthropic_beta
                .as_deref()
                .unwrap()
                .contains("claude-code-20250219")
        );
        assert_eq!(metadata.anthropic_version.as_deref(), Some("2023-06-01"));
        assert!(!metadata.features.is_empty());
    }

    #[test]
    fn strip_tool_references_removes_blocks() {
        let mut body = json!({
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Hello"},
                        {"type": "tool_reference", "tool_name": "search"},
                        {"type": "text", "text": "World"}
                    ]
                }
            ]
        });

        ClaudeCodeClient::strip_tool_references(&mut body);

        let content = body["messages"][0]["content"].as_array().unwrap();
        assert_eq!(content.len(), 2);
        assert_eq!(content[0]["type"], "text");
        assert_eq!(content[1]["type"], "text");
    }

    #[test]
    fn strip_tool_references_preserves_string_content() {
        let mut body = json!({
            "messages": [
                {"role": "user", "content": "Just a string"}
            ]
        });
        let original = body.clone();
        ClaudeCodeClient::strip_tool_references(&mut body);
        assert_eq!(body, original);
    }

    #[test]
    fn preprocess_strips_tool_reference_for_bedrock() {
        let mut body = json!({
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "tool_reference", "tool_name": "search"}
                ]
            }]
        });
        let metadata = ClientMetadata {
            client_name: "claude_code".into(),
            anthropic_beta: None,
            anthropic_version: None,
            extra_headers: HashMap::new(),
            features: HashSet::new(),
        };

        ClaudeCodeClient.preprocess_request(&mut body, &metadata, "bedrock");
        let content = body["messages"][0]["content"].as_array().unwrap();
        assert_eq!(content.len(), 1);
        assert_eq!(content[0]["type"], "text");
    }

    #[test]
    fn preprocess_keeps_tool_reference_for_anthropic_direct() {
        let mut body = json!({
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hello"},
                    {"type": "tool_reference", "tool_name": "search"}
                ]
            }]
        });
        let metadata = ClientMetadata {
            client_name: "claude_code".into(),
            anthropic_beta: None,
            anthropic_version: None,
            extra_headers: HashMap::new(),
            features: HashSet::new(),
        };

        ClaudeCodeClient.preprocess_request(&mut body, &metadata, "anthropic_direct");
        let content = body["messages"][0]["content"].as_array().unwrap();
        assert_eq!(content.len(), 2);
    }
}
