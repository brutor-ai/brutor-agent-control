// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! MiniMax (M2.x text family) adapter.
//!
//! ``api.minimax.io`` exposes a strict OpenAI-compatible Chat
//! Completions surface — confirmed against the published OpenAPI
//! schema at /api-reference/text/api/openapi-chat-openai.json.
//!
//! Behaviour notes that informed the drop list:
//!
//! * MiniMax models always emit a ``<think>...</think>`` block before
//!   the visible answer when reasoning is engaged. This stays in the
//!   ``content`` field — no special parsing needed by the proxy.
//! * The MiniMax response envelope adds ``base_resp.status_code`` and
//!   ``input_sensitive`` / ``output_sensitive`` fields. We don't
//!   normalise these away because OpenAI clients ignore unknown keys.
//! * MiniMax accepts ``name`` on each message for role-play scenarios
//!   (``{"role": "user", "name": "User", ...}``); standard OpenAI
//!   shape works too. No transformation needed.

use super::LlmAdapter;

pub struct MinimaxAdapter;

impl LlmAdapter for MinimaxAdapter {
    fn provider_name(&self) -> &str {
        "minimax"
    }

    // Provider-wide drop list (logprobs/top_logprobs/logit_bias/
    // parallel_tool_calls/suffix/stream_options) migrated to
    // `llm_providers.additional_drop_params`. See the
    // minimax entry in `init_llm_providers.PROVIDER_SEED_CATALOG`.
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn minimax_provider_name() {
        assert_eq!(MinimaxAdapter.provider_name(), "minimax");
    }

    // minimax_unsupported_params_pinned test removed — drop list moved
    // to the provider seed (covered by `services::providers` tests).

    #[test]
    fn minimax_default_transforms_are_passthrough() {
        // MiniMax is OpenAI-compatible; the adapter must not mutate
        // request bodies. Pin this so future drift to per-model
        // transformations is intentional.
        let adapter = MinimaxAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert(
            "messages".into(),
            serde_json::json!([
                {"role": "system", "name": "MiniMax AI"},
                {"role": "user", "name": "User", "content": "hi"}
            ]),
        );
        obj.insert("max_tokens".into(), serde_json::json!(128));
        obj.insert("temperature".into(), serde_json::json!(0.7));
        let original = obj.clone();

        let model = crate::services::llm_adapter::make_test_model("minimax", "MiniMax-M2.7");
        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj, original, "default adapter must be passthrough");
    }
}
