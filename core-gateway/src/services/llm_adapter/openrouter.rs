// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use serde_json::Value;

use super::LlmAdapter;
use crate::models::db::LlmModel;

pub struct OpenRouterAdapter;

impl LlmAdapter for OpenRouterAdapter {
    fn provider_name(&self) -> &str {
        "openrouter"
    }

    fn transform_request(&self, obj: &mut serde_json::Map<String, Value>, _model: &LlmModel) {
        // max_completion_tokens → max_tokens
        if let Some(mct) = obj.remove("max_completion_tokens") {
            if !obj.contains_key("max_tokens") {
                obj.insert("max_tokens".into(), mct);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::llm_adapter::make_test_model;
    use serde_json::json;

    #[test]
    fn openrouter_provider_name() {
        assert_eq!(OpenRouterAdapter.provider_name(), "openrouter");
    }

    #[test]
    fn openrouter_unsupported_params_empty() {
        assert!(OpenRouterAdapter.unsupported_params().is_empty());
    }

    #[test]
    fn openrouter_max_completion_tokens_to_max_tokens() {
        let model = make_test_model("openrouter", "meta-llama/llama-3-70b");
        let mut obj = serde_json::Map::new();
        obj.insert("max_completion_tokens".into(), json!(1024));

        OpenRouterAdapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_tokens"), Some(&json!(1024)));
        assert!(!obj.contains_key("max_completion_tokens"));
    }

    #[test]
    fn openrouter_does_not_overwrite_existing_max_tokens() {
        let model = make_test_model("openrouter", "meta-llama/llama-3-70b");
        let mut obj = serde_json::Map::new();
        obj.insert("max_completion_tokens".into(), json!(1024));
        obj.insert("max_tokens".into(), json!(512));

        OpenRouterAdapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_tokens"), Some(&json!(512)));
        assert!(!obj.contains_key("max_completion_tokens"));
    }
}
