// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use serde_json::Value;

use super::LlmAdapter;
use crate::models::db::LlmModel;

// ---------------------------------------------------------------------------
// OpenAI adapter (default / passthrough)
// ---------------------------------------------------------------------------

pub struct OpenAiAdapter;

impl LlmAdapter for OpenAiAdapter {
    fn provider_name(&self) -> &str {
        "openai"
    }

    fn transform_request(&self, obj: &mut serde_json::Map<String, Value>, model: &LlmModel) {
        // Which model takes `max_completion_tokens`, refuses sampling
        // parameters or needs `reasoning_effort: "none"` with tools is one
        // definition, shared with Azure OpenAI and read from the catalog row
        // first (`openai_family`).
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
    fn openai_gpt4o_max_tokens_to_max_completion_tokens() {
        let model = make_test_model("openai", "gpt-4o");
        let adapter = OpenAiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("max_tokens".into(), json!(1000));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_completion_tokens"), Some(&json!(1000)));
        assert!(!obj.contains_key("max_tokens"));
    }

    #[test]
    fn openai_o1_model_max_tokens_to_max_completion_tokens() {
        let model = make_test_model("openai", "o1-preview");
        let adapter = OpenAiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("max_tokens".into(), json!(2000));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_completion_tokens"), Some(&json!(2000)));
        assert!(!obj.contains_key("max_tokens"));
    }

    #[test]
    fn openai_o3_model_max_tokens_to_max_completion_tokens() {
        let model = make_test_model("openai", "o3-mini");
        let adapter = OpenAiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("max_tokens".into(), json!(500));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_completion_tokens"), Some(&json!(500)));
    }

    #[test]
    fn openai_o4_model_max_tokens_to_max_completion_tokens() {
        let model = make_test_model("openai", "o4-mini");
        let adapter = OpenAiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("max_tokens".into(), json!(800));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_completion_tokens"), Some(&json!(800)));
    }

    #[test]
    fn openai_regular_model_keeps_max_tokens() {
        let model = make_test_model("openai", "gpt-3.5-turbo");
        let adapter = OpenAiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("max_tokens".into(), json!(1000));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_tokens"), Some(&json!(1000)));
        assert!(!obj.contains_key("max_completion_tokens"));
    }

    #[test]
    fn openai_does_not_overwrite_existing_max_completion_tokens() {
        let model = make_test_model("openai", "gpt-4o");
        let adapter = OpenAiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("max_tokens".into(), json!(1000));
        obj.insert("max_completion_tokens".into(), json!(500));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_completion_tokens"), Some(&json!(500)));
    }

    #[test]
    fn openai_gpt5_drops_temperature_and_top_p() {
        let model = make_test_model("openai", "gpt-5-mini");
        let adapter = OpenAiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("temperature".into(), json!(0.7));
        obj.insert("top_p".into(), json!(0.9));
        obj.insert("max_tokens".into(), json!(100));

        adapter.transform_request(&mut obj, &model);

        assert!(!obj.contains_key("temperature"));
        assert!(!obj.contains_key("top_p"));
        assert_eq!(obj.get("max_completion_tokens"), Some(&json!(100)));
    }

    #[test]
    fn openai_o1_drops_temperature_and_top_p() {
        let model = make_test_model("openai", "o1-preview");
        let adapter = OpenAiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("temperature".into(), json!(0.5));
        obj.insert("top_p".into(), json!(0.8));

        adapter.transform_request(&mut obj, &model);

        assert!(!obj.contains_key("temperature"));
        assert!(!obj.contains_key("top_p"));
    }

    #[test]
    fn openai_gpt4o_keeps_temperature() {
        let model = make_test_model("openai", "gpt-4o");
        let adapter = OpenAiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("temperature".into(), json!(0.7));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("temperature"), Some(&json!(0.7)));
    }

    // -----------------------------------------------------------------------
    // OpenAI adapter — thinking_enabled (generic flag → reasoning_effort)
    // -----------------------------------------------------------------------

    #[test]
    fn openai_thinking_enabled_injects_reasoning_effort() {
        // A non-native-reasoning model (gpt-4o) with thinking_enabled=true
        // should still get reasoning_effort + the strip behaviour, exercising
        // the generic flag path independently of the model-name predicate.
        let mut model = make_test_model("openai", "gpt-4o");
        model.thinking_enabled = Some(true);
        let adapter = OpenAiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("temperature".into(), json!(0.5));
        obj.insert("top_p".into(), json!(0.9));
        adapter.transform_request(&mut obj, &model);
        assert_eq!(obj.get("reasoning_effort"), Some(&json!("medium")));
        assert!(!obj.contains_key("temperature"));
        assert!(!obj.contains_key("top_p"));
    }

    #[test]
    fn openai_thinking_enabled_preserves_caller_reasoning_effort() {
        // If the caller pinned reasoning_effort=high, we must not downgrade
        // them to "medium". The model-row flag is a default, not an override.
        let mut model = make_test_model("openai", "o3-mini");
        model.thinking_enabled = Some(true);
        let adapter = OpenAiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("reasoning_effort".into(), json!("high"));
        adapter.transform_request(&mut obj, &model);
        assert_eq!(obj.get("reasoning_effort"), Some(&json!("high")));
    }

    #[test]
    fn openai_thinking_disabled_on_chat_model_keeps_temperature() {
        // Negative: thinking off + a non-reasoning model = no transformation.
        let mut model = make_test_model("openai", "gpt-4o");
        model.thinking_enabled = Some(false);
        let adapter = OpenAiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("temperature".into(), json!(0.5));
        adapter.transform_request(&mut obj, &model);
        assert_eq!(obj.get("temperature"), Some(&json!(0.5)));
        assert!(!obj.contains_key("reasoning_effort"));
    }

    #[test]
    fn openai_native_reasoning_models_get_the_strip_even_without_flag() {
        // Native reasoning models (o1/o3/o4/gpt-5) always need the strip,
        // regardless of the model row's thinking_enabled column. Catches
        // accidental tightening of the strip predicate to "flag-only".
        // This used to assert an injected `reasoning_effort: medium`
        // too; no flag now means no injected effort (the provider default).
        let model = make_test_model("openai", "o3-mini");
        // thinking_enabled stays None.
        let adapter = OpenAiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("temperature".into(), json!(0.5));
        adapter.transform_request(&mut obj, &model);
        assert!(!obj.contains_key("reasoning_effort"));
        assert!(!obj.contains_key("temperature"));
    }

    #[test]
    fn openai_does_not_invent_reasoning_effort_for_a_native_reasoning_model() {
        // The adapter added `reasoning_effort: medium` to every
        // gpt-5 / o-series call nobody asked to reason harder on. With
        // function tools, gpt-5.4-nano refuses that on Chat Completions
        // (400 "Function tools with reasoning_effort are not supported"),
        // and on every other model it overrode the provider's own default.
        // The strip stays: these models still reject temperature/top_p.
        let model = make_test_model("openai", "gpt-5.4-nano");
        let adapter = OpenAiAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("temperature".into(), json!(0.5));
        obj.insert(
            "tools".into(),
            json!([{"type": "function", "function": {"name": "f", "parameters": {}}}]),
        );
        adapter.transform_request(&mut obj, &model);
        assert!(!obj.contains_key("reasoning_effort"), "{obj:?}");
        assert!(!obj.contains_key("temperature"));
    }

    // -----------------------------------------------------------------------
    // The current catalog: GPT-6
    // -----------------------------------------------------------------------

    #[test]
    fn openai_gpt6_takes_max_completion_tokens_and_no_sampling() {
        for name in ["gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"] {
            let model = make_test_model("openai", name);
            let mut obj = serde_json::Map::new();
            obj.insert("temperature".into(), json!(0.7));
            obj.insert("top_p".into(), json!(0.9));
            obj.insert("max_tokens".into(), json!(100));
            OpenAiAdapter.transform_request(&mut obj, &model);
            assert_eq!(
                obj.get("max_completion_tokens"),
                Some(&json!(100)),
                "{name}"
            );
            assert!(!obj.contains_key("max_tokens"), "{name}");
            assert!(!obj.contains_key("temperature"), "{name}");
            assert!(!obj.contains_key("top_p"), "{name}");
            assert!(!obj.contains_key("reasoning_effort"), "{name}");
        }
    }

    #[test]
    fn openai_gpt6_tools_get_reasoning_effort_none_when_the_caller_chose_none() {
        let tools = json!([{"type": "function", "function": {"name": "f", "parameters": {}}}]);
        for name in ["gpt-6-sol", "gpt-6-luna"] {
            let model = make_test_model("openai", name);
            let mut obj = serde_json::Map::new();
            obj.insert("tools".into(), tools.clone());
            OpenAiAdapter.transform_request(&mut obj, &model);
            assert_eq!(obj.get("reasoning_effort"), Some(&json!("none")), "{name}");
        }
        // No `none` level on these two: nothing is invented.
        for name in ["gpt-6.1-sol", "gpt-6-astra", "gpt-5.4-nano"] {
            let model = make_test_model("openai", name);
            let mut obj = serde_json::Map::new();
            obj.insert("tools".into(), tools.clone());
            OpenAiAdapter.transform_request(&mut obj, &model);
            assert!(!obj.contains_key("reasoning_effort"), "{name}");
        }
    }
}
