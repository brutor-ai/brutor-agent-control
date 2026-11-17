// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use serde_json::Value;

use super::LlmAdapter;
use crate::models::db::LlmModel;

pub struct AzureAdapter;

impl LlmAdapter for AzureAdapter {
    fn provider_name(&self) -> &str {
        "azure"
    }

    fn auth_headers(&self, model: &LlmModel) -> Vec<(String, String)> {
        let mut headers = Vec::new();
        if let Some(ref key) = model.api_key {
            headers.push(("api-key".into(), key.clone()));
        }
        headers
    }

    fn transform_request(&self, obj: &mut serde_json::Map<String, Value>, model: &LlmModel) {
        // Azure OpenAI serves the OpenAI families under the same parameter
        // rules. They are one definition (`openai_family`), which reads the
        // family from the name after the catalog's `azure/` prefix: the
        // check here used to be `name.starts_with("gpt-5")` on ids seeded as
        // `azure/gpt-5.2`, which no seeded Azure model ever matched.
        super::openai_family::apply(obj, model);
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::llm_adapter::make_test_model;
    use serde_json::json;

    #[test]
    fn azure_auth_headers_uses_api_key() {
        let model = make_test_model("azure", "gpt-4o");
        let adapter = AzureAdapter;
        let headers = adapter.auth_headers(&model);

        let has_api_key = headers
            .iter()
            .any(|(k, v)| k == "api-key" && v == "sk-test-key");
        assert!(has_api_key, "expected api-key header");

        let has_no_bearer = headers.iter().all(|(k, _)| k != "Authorization");
        assert!(has_no_bearer, "should not have Authorization header");
    }

    #[test]
    fn azure_gpt4o_max_tokens_to_max_completion_tokens() {
        let model = make_test_model("azure", "gpt-4o");
        let adapter = AzureAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("max_tokens".into(), json!(1000));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_completion_tokens"), Some(&json!(1000)));
        assert!(!obj.contains_key("max_tokens"));
    }

    #[test]
    fn azure_regular_model_keeps_max_tokens() {
        let model = make_test_model("azure", "gpt-35-turbo");
        let adapter = AzureAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("max_tokens".into(), json!(1000));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_tokens"), Some(&json!(1000)));
        assert!(!obj.contains_key("max_completion_tokens"));
    }

    // -----------------------------------------------------------------------
    // The current catalog
    // -----------------------------------------------------------------------

    #[test]
    fn azure_seeded_ids_carry_a_prefix_the_family_rule_reads_past() {
        // Every Azure OpenAI row is seeded as `azure/<model>`.
        for name in ["azure/gpt-5.2", "azure/gpt-5.1", "azure/gpt-5.6-sol"] {
            let model = make_test_model("azure", name);
            let mut obj = serde_json::Map::new();
            obj.insert("max_tokens".into(), json!(1000));
            obj.insert("temperature".into(), json!(0.7));
            obj.insert("top_p".into(), json!(0.9));
            AzureAdapter.transform_request(&mut obj, &model);
            assert_eq!(
                obj.get("max_completion_tokens"),
                Some(&json!(1000)),
                "{name}"
            );
            assert!(!obj.contains_key("max_tokens"), "{name}");
            // Microsoft: reasoning models refuse both.
            assert!(!obj.contains_key("temperature"), "{name}");
            assert!(!obj.contains_key("top_p"), "{name}");
        }
    }

    #[test]
    fn azure_gpt56_tools_get_reasoning_effort_none() {
        let model = make_test_model("azure", "azure/gpt-5.6-luna");
        let mut obj = serde_json::Map::new();
        obj.insert("tools".into(), json!([{"type": "function"}]));
        AzureAdapter.transform_request(&mut obj, &model);
        assert_eq!(obj.get("reasoning_effort"), Some(&json!("none")));
    }
}
