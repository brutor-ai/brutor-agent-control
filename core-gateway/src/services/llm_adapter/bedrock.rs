// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use serde_json::{Value, json};

use super::LlmAdapter;
use crate::models::db::LlmModel;

pub struct BedrockAdapter;

impl LlmAdapter for BedrockAdapter {
    fn provider_name(&self) -> &str {
        "bedrock"
    }

    fn transform_request(&self, obj: &mut serde_json::Map<String, Value>, _model: &LlmModel) {
        // reasoning_effort → thinking parameter with budget
        if let Some(re) = obj.remove("reasoning_effort") {
            if let Some(effort) = re.as_str() {
                let budget = match effort {
                    "low" | "minimal" => 1024,
                    "medium" => 10000,
                    "high" => 15000,
                    _ => 10000,
                };
                if !obj.contains_key("thinking") {
                    obj.insert(
                        "thinking".into(),
                        json!({
                            "type": "enabled",
                            "budget_tokens": budget
                        }),
                    );
                }
            }
        }

        // Clamp thinking budget_tokens to minimum 1024
        if let Some(thinking) = obj.get_mut("thinking") {
            if let Some(budget) = thinking.get("budget_tokens").and_then(|b| b.as_i64()) {
                if budget < 1024 {
                    if let Some(obj) = thinking.as_object_mut() {
                        obj.insert("budget_tokens".into(), Value::from(1024));
                    }
                }
            }
        }
    }

    // Provider-wide drop list migrated to
    // `llm_providers.additional_drop_params` — see the
    // bedrock / bedrock_converse entries in
    // `init_llm_providers.PROVIDER_SEED_CATALOG`.
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
    fn bedrock_reasoning_effort_low_to_thinking() {
        let model = make_test_model("bedrock", "anthropic.claude-3-5-sonnet");
        let adapter = BedrockAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("reasoning_effort".into(), json!("low"));

        adapter.transform_request(&mut obj, &model);

        assert!(!obj.contains_key("reasoning_effort"));
        let thinking = obj.get("thinking").unwrap();
        assert_eq!(thinking.get("type").unwrap(), "enabled");
        assert_eq!(thinking.get("budget_tokens").unwrap(), 1024);
    }

    #[test]
    fn bedrock_reasoning_effort_high_to_thinking() {
        let model = make_test_model("bedrock", "anthropic.claude-3-5-sonnet");
        let adapter = BedrockAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("reasoning_effort".into(), json!("high"));

        adapter.transform_request(&mut obj, &model);

        let thinking = obj.get("thinking").unwrap();
        assert_eq!(thinking.get("type").unwrap(), "enabled");
        assert_eq!(thinking.get("budget_tokens").unwrap(), 15000);
    }

    #[test]
    fn bedrock_clamps_budget_tokens_minimum() {
        let model = make_test_model("bedrock", "anthropic.claude-3-5-sonnet");
        let adapter = BedrockAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert(
            "thinking".into(),
            json!({
                "type": "enabled",
                "budget_tokens": 500
            }),
        );

        adapter.transform_request(&mut obj, &model);

        let thinking = obj.get("thinking").unwrap();
        assert_eq!(thinking.get("budget_tokens").unwrap(), 1024);
    }
}
