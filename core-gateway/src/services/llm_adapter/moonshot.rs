// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Moonshot AI (Kimi K2.x + Moonshot V1) adapter.
//!
//! ``api.moonshot.ai`` exposes a strict OpenAI-compatible Chat
//! Completions surface, so this adapter is a near-passthrough — the
//! only deviation from canonical OpenAI is a small list of params
//! Moonshot rejects with a 400. Everything else (messages, tools,
//! tool_choice, response_format, stream, max_tokens) flows through
//! unchanged.

use super::LlmAdapter;

pub struct MoonshotAdapter;

impl LlmAdapter for MoonshotAdapter {
    fn provider_name(&self) -> &str {
        "moonshot"
    }

    // Provider-wide drop list migrated to
    // `llm_providers.additional_drop_params` — see the
    // moonshot entry in `init_llm_providers.PROVIDER_SEED_CATALOG`.
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn moonshot_provider_name() {
        assert_eq!(MoonshotAdapter.provider_name(), "moonshot");
    }

    // moonshot_unsupported_params test removed — drop list moved to
    // the provider seed (covered by `services::providers` tests).

    #[test]
    fn moonshot_default_transforms_are_passthrough() {
        // Moonshot is OpenAI-compatible; the adapter must not mutate
        // request bodies (messages/tools/etc. survive untouched). If
        // we ever add transformations, this test should grow assertions.
        let adapter = MoonshotAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert(
            "messages".into(),
            serde_json::json!([{"role": "user", "content": "hi"}]),
        );
        obj.insert("max_tokens".into(), serde_json::json!(64));
        obj.insert("temperature".into(), serde_json::json!(0.5));
        let original = obj.clone();

        let model = crate::services::llm_adapter::make_test_model("moonshot", "kimi-k2.6");
        adapter.transform_request(&mut obj, &model);

        assert_eq!(obj, original, "default adapter must be passthrough");
    }
}
