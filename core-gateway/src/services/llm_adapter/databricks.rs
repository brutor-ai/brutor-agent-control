// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use serde_json::Value;

use super::LlmAdapter;
use crate::models::db::LlmModel;

pub struct DatabricksAdapter;

impl LlmAdapter for DatabricksAdapter {
    fn provider_name(&self) -> &str {
        "databricks"
    }

    fn transform_request(&self, obj: &mut serde_json::Map<String, Value>, _model: &LlmModel) {
        // max_completion_tokens → max_tokens
        if let Some(mct) = obj.remove("max_completion_tokens") {
            if !obj.contains_key("max_tokens") {
                obj.insert("max_tokens".into(), mct);
            }
        }

        // Strip name from user messages
        if let Some(messages) = obj.get_mut("messages").and_then(|m| m.as_array_mut()) {
            for msg in messages.iter_mut() {
                let role = msg.get("role").and_then(|r| r.as_str()).unwrap_or("");
                if role == "user" {
                    if let Some(msg_obj) = msg.as_object_mut() {
                        msg_obj.remove("name");
                    }
                }
            }
        }
    }

    // Provider-wide drop list migrated to
    // `llm_providers.additional_drop_params` — see the
    // databricks entry in `init_llm_providers.PROVIDER_SEED_CATALOG`.
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::llm_adapter::make_test_model;
    use serde_json::json;

    #[test]
    fn databricks_provider_name() {
        assert_eq!(DatabricksAdapter.provider_name(), "databricks");
    }

    // databricks_unsupported_params test removed — drop list moved to
    // the provider seed (covered by `services::providers` tests).

    #[test]
    fn databricks_max_completion_tokens_to_max_tokens() {
        let model = make_test_model("databricks", "dbrx-instruct");
        let mut obj = serde_json::Map::new();
        obj.insert("max_completion_tokens".into(), json!(4096));

        DatabricksAdapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_tokens"), Some(&json!(4096)));
        assert!(!obj.contains_key("max_completion_tokens"));
    }

    #[test]
    fn databricks_does_not_overwrite_existing_max_tokens() {
        let model = make_test_model("databricks", "dbrx-instruct");
        let mut obj = serde_json::Map::new();
        obj.insert("max_completion_tokens".into(), json!(4096));
        obj.insert("max_tokens".into(), json!(2048));

        DatabricksAdapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("max_tokens"), Some(&json!(2048)));
        assert!(!obj.contains_key("max_completion_tokens"));
    }

    #[test]
    fn databricks_strips_name_from_user_messages_only() {
        let model = make_test_model("databricks", "dbrx-instruct");
        let mut obj = serde_json::Map::new();
        obj.insert(
            "messages".into(),
            json!([
                {"role": "user", "content": "hi", "name": "alice"},
                {"role": "assistant", "content": "hello", "name": "bot"},
                {"role": "system", "content": "you are helpful", "name": "sys"}
            ]),
        );

        DatabricksAdapter.transform_request(&mut obj, &model);

        let msgs = obj.get("messages").unwrap().as_array().unwrap();
        // user message: name stripped
        assert!(!msgs[0].as_object().unwrap().contains_key("name"));
        // assistant message: name kept
        assert!(msgs[1].as_object().unwrap().contains_key("name"));
        // system message: name kept
        assert!(msgs[2].as_object().unwrap().contains_key("name"));
    }
}
