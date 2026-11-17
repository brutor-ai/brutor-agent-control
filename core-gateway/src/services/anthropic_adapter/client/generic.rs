// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Generic Anthropic Client Adapter
//!
//! Default passthrough adapter for standard Anthropic SDK clients
//! and custom applications. Captures `anthropic-beta` and
//! `anthropic-version` headers with no body modifications.

use axum::http::HeaderMap;
use serde_json::Value;
use std::collections::{HashMap, HashSet};

use super::{ClientAdapter, ClientMetadata};

// ---------------------------------------------------------------------------
// GenericClient
// ---------------------------------------------------------------------------

pub struct GenericClient;

impl ClientAdapter for GenericClient {
    fn client_name(&self) -> &str {
        "generic"
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

        ClientMetadata {
            client_name: "generic".into(),
            anthropic_beta,
            anthropic_version,
            extra_headers: HashMap::new(),
            features: HashSet::new(),
        }
    }

    fn preprocess_request(
        &self,
        _body: &mut Value,
        _metadata: &ClientMetadata,
        _backend_name: &str,
    ) {
        // No-op: generic clients need no body modifications
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generic_client_name() {
        assert_eq!(GenericClient.client_name(), "generic");
    }

    #[test]
    fn generic_extracts_headers() {
        let mut headers = HeaderMap::new();
        headers.insert(
            "anthropic-beta",
            "interleaved-thinking-2025-01-24".parse().unwrap(),
        );
        headers.insert("anthropic-version", "2023-06-01".parse().unwrap());

        let metadata = GenericClient.extract_client_headers(&headers);
        assert_eq!(
            metadata.anthropic_beta.as_deref(),
            Some("interleaved-thinking-2025-01-24")
        );
        assert_eq!(metadata.anthropic_version.as_deref(), Some("2023-06-01"));
        assert!(metadata.features.is_empty());
    }

    #[test]
    fn generic_preprocess_is_noop() {
        let mut body = serde_json::json!({"model": "claude-sonnet-4-6", "messages": []});
        let original = body.clone();
        let metadata = ClientMetadata {
            client_name: "generic".into(),
            anthropic_beta: None,
            anthropic_version: None,
            extra_headers: HashMap::new(),
            features: HashSet::new(),
        };
        GenericClient.preprocess_request(&mut body, &metadata, "anthropic_direct");
        assert_eq!(body, original);
    }
}
