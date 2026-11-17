// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use serde_json::Value;

use super::LlmAdapter;
use crate::models::db::LlmModel;

pub struct ReplicateAdapter;

impl LlmAdapter for ReplicateAdapter {
    fn provider_name(&self) -> &str {
        "replicate"
    }

    fn auth_headers(&self, model: &LlmModel) -> Vec<(String, String)> {
        let mut headers = Vec::new();
        if let Some(ref key) = model.api_key {
            headers.push(("Authorization".into(), format!("Token {key}")));
        }
        headers
    }

    fn transform_request(&self, obj: &mut serde_json::Map<String, Value>, model: &LlmModel) {
        // max_tokens mapping depends on model
        if let Some(mt) = obj.remove("max_tokens") {
            let name = &model.model_name;
            if name.contains("vicuna") || name.contains("flan") {
                obj.insert("max_length".into(), mt);
            } else if name.contains("codellama") {
                obj.insert("max_tokens".into(), mt);
            } else {
                obj.insert("max_new_tokens".into(), mt);
            }
        }
        if let Some(mct) = obj.remove("max_completion_tokens") {
            if !obj.contains_key("max_new_tokens")
                && !obj.contains_key("max_length")
                && !obj.contains_key("max_tokens")
            {
                obj.insert("max_new_tokens".into(), mct);
            }
        }

        // stop → stop_sequences
        if let Some(stop) = obj.remove("stop") {
            if !obj.contains_key("stop_sequences") {
                let sequences = match stop {
                    Value::Array(_) => stop,
                    Value::String(s) => Value::Array(vec![Value::String(s)]),
                    _ => Value::Array(vec![]),
                };
                if let Some(arr) = sequences.as_array() {
                    if !arr.is_empty() {
                        obj.insert("stop_sequences".into(), sequences);
                    }
                }
            }
        }
    }

    // Provider-wide drop list migrated to
    // `llm_providers.additional_drop_params` — see the
    // replicate entry in `init_llm_providers.PROVIDER_SEED_CATALOG`.
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
    fn replicate_max_tokens_to_max_new_tokens_default() {
        let model = make_test_model("replicate", "llama-2-70b");
        let adapter = ReplicateAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("max_tokens".into(), json!(500));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_new_tokens"), Some(&json!(500)));
        assert!(!obj.contains_key("max_tokens"));
    }

    #[test]
    fn replicate_max_tokens_to_max_length_vicuna() {
        let model = make_test_model("replicate", "vicuna-13b");
        let adapter = ReplicateAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("max_tokens".into(), json!(500));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_length"), Some(&json!(500)));
        assert!(!obj.contains_key("max_tokens"));
        assert!(!obj.contains_key("max_new_tokens"));
    }

    #[test]
    fn replicate_stop_to_stop_sequences() {
        let model = make_test_model("replicate", "llama-2-70b");
        let adapter = ReplicateAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("stop".into(), json!(["END"]));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("stop_sequences"), Some(&json!(["END"])));
        assert!(!obj.contains_key("stop"));
    }
}
