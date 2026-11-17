// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use serde_json::Value;

use super::LlmAdapter;
use crate::models::db::LlmModel;

// ---------------------------------------------------------------------------
// Cohere adapter
// ---------------------------------------------------------------------------

pub struct CohereAdapter;

impl LlmAdapter for CohereAdapter {
    fn provider_name(&self) -> &str {
        "cohere"
    }

    fn transform_request(&self, obj: &mut serde_json::Map<String, Value>, _model: &LlmModel) {
        // n → num_generations
        if let Some(n) = obj.remove("n") {
            if !obj.contains_key("num_generations") {
                obj.insert("num_generations".into(), n);
            }
        }

        // top_p → p
        if let Some(tp) = obj.remove("top_p") {
            if !obj.contains_key("p") {
                obj.insert("p".into(), tp);
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

        // Remove unsupported params
        for param in [
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "parallel_tool_calls",
            "suffix",
            "user",
            "stream_options",
        ] {
            obj.remove(param);
        }
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
    fn cohere_n_to_num_generations() {
        let model = make_test_model("cohere", "command-r");
        let adapter = CohereAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("n".into(), json!(2));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("num_generations"), Some(&json!(2)));
        assert!(!obj.contains_key("n"));
    }

    #[test]
    fn cohere_top_p_to_p() {
        let model = make_test_model("cohere", "command-r");
        let adapter = CohereAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("top_p".into(), json!(0.9));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("p"), Some(&json!(0.9)));
        assert!(!obj.contains_key("top_p"));
    }

    #[test]
    fn cohere_stop_to_stop_sequences() {
        let model = make_test_model("cohere", "command-r");
        let adapter = CohereAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("stop".into(), json!(["DONE"]));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("stop_sequences"), Some(&json!(["DONE"])));
        assert!(!obj.contains_key("stop"));
    }
}
