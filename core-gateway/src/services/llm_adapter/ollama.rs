// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use serde_json::Value;

use super::LlmAdapter;
use crate::models::db::LlmModel;

pub struct OllamaAdapter;

impl LlmAdapter for OllamaAdapter {
    fn provider_name(&self) -> &str {
        "ollama"
    }

    /// Ollama's OpenAI-compatible API lives under /v1.
    /// The base api_url is http://host:11434 (no /v1), because
    /// the control plane's health check uses the native /api/embed path.
    /// We prepend /v1 here so the Rust proxy hits the right endpoint.
    fn chat_endpoint(&self, _model: &LlmModel, default: &str, _stream: bool) -> String {
        format!("/v1{default}")
    }

    fn transform_request(&self, obj: &mut serde_json::Map<String, Value>, _model: &LlmModel) {
        // max_tokens / max_completion_tokens → num_predict
        if let Some(mt) = obj.remove("max_tokens") {
            if !obj.contains_key("num_predict") {
                obj.insert("num_predict".into(), mt);
            }
        }
        if let Some(mct) = obj.remove("max_completion_tokens") {
            if !obj.contains_key("num_predict") {
                obj.insert("num_predict".into(), mct);
            }
        }
    }

    // Provider-wide drop list migrated to
    // `llm_providers.additional_drop_params` — see the
    // ollama entry in `init_llm_providers.PROVIDER_SEED_CATALOG`.
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
    fn ollama_max_tokens_to_num_predict() {
        let model = make_test_model("ollama", "llama3");
        let adapter = OllamaAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("max_tokens".into(), json!(500));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("num_predict"), Some(&json!(500)));
        assert!(!obj.contains_key("max_tokens"));
    }

    #[test]
    fn ollama_max_completion_tokens_to_num_predict() {
        let model = make_test_model("ollama", "llama3");
        let adapter = OllamaAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("max_completion_tokens".into(), json!(1000));

        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj.get("num_predict"), Some(&json!(1000)));
        assert!(!obj.contains_key("max_completion_tokens"));
    }
}
