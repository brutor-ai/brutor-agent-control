// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use serde_json::Value;

use super::{LlmAdapter, strip_name_from_messages, strip_strict_from_tools};
use crate::models::db::LlmModel;

pub struct XaiAdapter;

impl LlmAdapter for XaiAdapter {
    fn provider_name(&self) -> &str {
        "xai"
    }

    fn transform_request(&self, obj: &mut serde_json::Map<String, Value>, model: &LlmModel) {
        let _ = model;

        // max_completion_tokens → max_tokens
        if let Some(mct) = obj.remove("max_completion_tokens") {
            if !obj.contains_key("max_tokens") {
                obj.insert("max_tokens".into(), mct);
            }
        }

        // Strip name from all messages
        strip_name_from_messages(obj);

        // Strip strict from tools
        strip_strict_from_tools(obj);

        // Per-model parameter drops (`stop`, `frequency_penalty`) used
        // to live here as hardcoded `model.model_name.contains("grok-4")`
        // checks. They migrated to `llm_providers.additional_drop_params` —
        // see `init_llm_providers.PROVIDER_SEED_CATALOG`
        // (xai entry) and `services::providers::drop_unsupported_params`.
    }

    fn normalize_response(&self, body: &mut Value) {
        // Fix empty finish_reason when tool_calls present
        if let Some(choices) = body.get_mut("choices").and_then(|c| c.as_array_mut()) {
            for choice in choices.iter_mut() {
                let has_tool_calls = choice
                    .get("message")
                    .and_then(|m| m.get("tool_calls"))
                    .and_then(|tc| tc.as_array())
                    .map_or(false, |a| !a.is_empty());

                if has_tool_calls {
                    let fr = choice.get("finish_reason");
                    let is_empty = fr.map_or(true, |v| {
                        v.is_null() || v.as_str().map_or(false, |s| s.is_empty())
                    });
                    if is_empty {
                        if let Some(obj) = choice.as_object_mut() {
                            obj.insert("finish_reason".into(), Value::String("tool_calls".into()));
                        }
                    }
                }
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
    fn xai_max_completion_tokens_to_max_tokens() {
        let model = make_test_model("xai", "grok-2");
        let adapter = XaiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("max_completion_tokens".into(), json!(2048));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_tokens"), Some(&json!(2048)));
        assert!(!obj.contains_key("max_completion_tokens"));
    }

    #[test]
    fn xai_strips_name_from_messages() {
        let model = make_test_model("xai", "grok-2");
        let adapter = XaiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert(
            "messages".into(),
            json!([
                {"role": "user", "content": "hi", "name": "alice"},
                {"role": "assistant", "content": "hello", "name": "bot"}
            ]),
        );

        adapter.transform_request(&mut obj, &model);

        let msgs = obj.get("messages").unwrap().as_array().unwrap();
        for msg in msgs {
            assert!(!msg.as_object().unwrap().contains_key("name"));
        }
    }

    #[test]
    fn xai_strips_strict_from_tools() {
        let model = make_test_model("xai", "grok-2");
        let adapter = XaiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert(
            "tools".into(),
            json!([{
                "type": "function",
                "strict": true,
                "function": {
                    "name": "get_weather",
                    "strict": true,
                    "parameters": {"type": "object"}
                }
            }]),
        );

        adapter.transform_request(&mut obj, &model);

        let tool = &obj.get("tools").unwrap().as_array().unwrap()[0];
        assert!(!tool.as_object().unwrap().contains_key("strict"));
        assert!(
            !tool
                .get("function")
                .unwrap()
                .as_object()
                .unwrap()
                .contains_key("strict")
        );
    }

    #[test]
    fn xai_finish_reason_fix_empty_string_with_tool_calls() {
        let adapter = XaiAdapter;
        let mut body = json!({
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": null,
                    "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "test", "arguments": "{}"}}]
                },
                "finish_reason": ""
            }]
        });

        adapter.normalize_response(&mut body);

        assert_eq!(body["choices"][0]["finish_reason"], "tool_calls");
    }

    // Per-model parameter drops (stop, frequency_penalty) moved to
    // `llm_providers.additional_drop_params` — coverage
    // now lives in `services::providers::drop_unsupported_params`
    // tests in `services/providers.rs`.
}
