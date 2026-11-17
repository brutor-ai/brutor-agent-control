// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use serde_json::Value;

use super::LlmAdapter;
use crate::models::db::LlmModel;

pub struct MistralAdapter;

impl LlmAdapter for MistralAdapter {
    fn provider_name(&self) -> &str {
        "mistral"
    }

    fn transform_request(&self, obj: &mut serde_json::Map<String, Value>, _model: &LlmModel) {
        // max_completion_tokens takes priority → max_tokens
        if let Some(mct) = obj.remove("max_completion_tokens") {
            obj.insert("max_tokens".into(), mct);
        }

        // tool_choice: "required" → "any", object → "any"
        if let Some(tc) = obj.get("tool_choice").cloned() {
            match &tc {
                Value::String(s) if s == "required" => {
                    obj.insert("tool_choice".into(), Value::String("any".into()));
                }
                Value::Object(_) => {
                    // Mistral doesn't support function-specific tool_choice
                    obj.insert("tool_choice".into(), Value::String("any".into()));
                }
                _ => {} // "auto", "none" pass through
            }
        }

        // Clean tool definitions: remove $id, $schema, additionalProperties, strict
        if let Some(tools) = obj.get_mut("tools").and_then(|t| t.as_array_mut()) {
            for tool in tools.iter_mut() {
                self.clean_tool(tool);
            }
        }

        // Strip name field from non-tool messages
        if let Some(messages) = obj.get_mut("messages").and_then(|m| m.as_array_mut()) {
            for msg in messages.iter_mut() {
                let role = msg.get("role").and_then(|r| r.as_str()).unwrap_or("");
                if role != "tool" {
                    if let Some(msg_obj) = msg.as_object_mut() {
                        msg_obj.remove("name");
                    }
                }
            }
            // Remove empty assistant messages
            messages.retain(|msg| {
                let role = msg.get("role").and_then(|r| r.as_str()).unwrap_or("");
                if role == "assistant" {
                    let content = msg.get("content");
                    let is_empty = content
                        .map(|c| c.as_str().map_or(false, |s| s.is_empty()) || c.is_null())
                        .unwrap_or(true);
                    let has_tool_calls = msg.get("tool_calls").is_some();
                    !is_empty || has_tool_calls
                } else {
                    true
                }
            });
        }
    }

    // Provider-wide drop list migrated to
    // `llm_providers.additional_drop_params` — see the
    // mistral entry in `init_llm_providers.PROVIDER_SEED_CATALOG`.
}

impl MistralAdapter {
    fn clean_tool(&self, tool: &mut Value) {
        if let Some(obj) = tool.as_object_mut() {
            obj.remove("strict");
            if let Some(func) = obj.get_mut("function") {
                if let Some(func_obj) = func.as_object_mut() {
                    func_obj.remove("strict");
                    if let Some(params) = func_obj.get_mut("parameters") {
                        self.clean_schema(params);
                    }
                }
            }
        }
    }

    fn clean_schema(&self, schema: &mut Value) {
        if let Some(obj) = schema.as_object_mut() {
            obj.remove("$id");
            obj.remove("$schema");
            obj.remove("additionalProperties");
            obj.remove("strict");
            // Recurse into properties
            if let Some(props) = obj.get_mut("properties") {
                if let Some(props_obj) = props.as_object_mut() {
                    for (_, v) in props_obj.iter_mut() {
                        self.clean_schema(v);
                    }
                }
            }
            if let Some(items) = obj.get_mut("items") {
                self.clean_schema(items);
            }
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
    fn mistral_tool_choice_required_to_any() {
        let model = make_test_model("mistral", "mistral-large");
        let adapter = MistralAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("tool_choice".into(), json!("required"));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("tool_choice"), Some(&json!("any")));
    }

    #[test]
    fn mistral_tool_choice_object_to_any() {
        let model = make_test_model("mistral", "mistral-large");
        let adapter = MistralAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert(
            "tool_choice".into(),
            json!({"type": "function", "function": {"name": "get_weather"}}),
        );

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("tool_choice"), Some(&json!("any")));
    }

    #[test]
    fn mistral_tool_choice_auto_passes_through() {
        let model = make_test_model("mistral", "mistral-large");
        let adapter = MistralAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("tool_choice".into(), json!("auto"));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("tool_choice"), Some(&json!("auto")));
    }

    #[test]
    fn mistral_max_completion_tokens_to_max_tokens() {
        let model = make_test_model("mistral", "mistral-large");
        let adapter = MistralAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("max_completion_tokens".into(), json!(2048));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_tokens"), Some(&json!(2048)));
        assert!(!obj.contains_key("max_completion_tokens"));
    }

    #[test]
    fn mistral_cleans_tool_parameters() {
        let model = make_test_model("mistral", "mistral-large");
        let adapter = MistralAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert(
            "tools".into(),
            json!([{
                "type": "function",
                "strict": true,
                "function": {
                    "name": "get_weather",
                    "strict": true,
                    "parameters": {
                        "type": "object",
                        "$id": "some-id",
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "additionalProperties": false,
                        "strict": true,
                        "properties": {
                            "city": {
                                "type": "string",
                                "$id": "nested-id",
                                "additionalProperties": false
                            }
                        }
                    }
                }
            }]),
        );

        adapter.transform_request(&mut obj, &model);

        let tools = obj.get("tools").unwrap().as_array().unwrap();
        let tool = &tools[0];
        assert!(!tool.as_object().unwrap().contains_key("strict"));
        let func = tool.get("function").unwrap();
        assert!(!func.as_object().unwrap().contains_key("strict"));
        let params = func.get("parameters").unwrap();
        assert!(!params.as_object().unwrap().contains_key("$id"));
        assert!(!params.as_object().unwrap().contains_key("$schema"));
        assert!(
            !params
                .as_object()
                .unwrap()
                .contains_key("additionalProperties")
        );
        assert!(!params.as_object().unwrap().contains_key("strict"));
        let city = params.get("properties").unwrap().get("city").unwrap();
        assert!(!city.as_object().unwrap().contains_key("$id"));
        assert!(
            !city
                .as_object()
                .unwrap()
                .contains_key("additionalProperties")
        );
    }
}
