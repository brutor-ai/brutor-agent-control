// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use super::{LlmAdapter, StreamNormalizer};
use serde_json::Value;

pub struct GroqAdapter;

impl LlmAdapter for GroqAdapter {
    fn provider_name(&self) -> &str {
        "groq"
    }

    // Provider-wide drop list migrated to
    // `llm_providers.additional_drop_params` — see the
    // groq entry in `init_llm_providers.PROVIDER_SEED_CATALOG`.

    /// Groq's `compound` family (and any future thinking models on
    /// the platform) emit chain-of-thought trace under
    /// `delta.reasoning` instead of the OpenAI/DeepSeek/MiniMax
    /// canonical `delta.reasoning_content`. We normalise inline so
    /// every downstream consumer sees a single field name.
    fn needs_stream_normalization(&self) -> bool {
        true
    }

    fn create_stream_normalizer(&self) -> Box<dyn StreamNormalizer> {
        Box::new(GroqStreamNormalizer)
    }

    /// Same alias for non-streaming responses.
    fn normalize_response(&self, body: &mut Value) {
        normalize_reasoning_alias_in_choices(body);
    }
}

/// Rewrite `delta.reasoning` → `delta.reasoning_content` on each
/// streaming chunk. Pass-through for any frame that doesn't carry
/// the divergent field — this is the lightest possible
/// normaliser, contrasted with Anthropic's full event-stream
/// translation.
pub struct GroqStreamNormalizer;

impl StreamNormalizer for GroqStreamNormalizer {
    fn convert(&mut self, data: &str) -> Vec<String> {
        // Sentinel — terminating frame in OpenAI-compat SSE. Pass
        // through unchanged so downstream parsers see the close.
        if data == "[DONE]" {
            return vec![data.to_string()];
        }

        let mut parsed: Value = match serde_json::from_str(data) {
            Ok(v) => v,
            // Bad JSON in a chunk — forward verbatim so the issue
            // surfaces to the operator rather than being silently
            // swallowed by us.
            Err(_) => return vec![data.to_string()],
        };

        if rewrite_reasoning_field_in_choices(&mut parsed) {
            return vec![parsed.to_string()];
        }
        vec![data.to_string()]
    }
}

/// Walk `choices[].delta` and rename `reasoning` → `reasoning_content`.
/// Returns true when a rewrite happened so the caller can avoid
/// re-serialising untouched chunks.
fn rewrite_reasoning_field_in_choices(value: &mut Value) -> bool {
    let Some(choices) = value.get_mut("choices").and_then(|c| c.as_array_mut()) else {
        return false;
    };
    let mut touched = false;
    for choice in choices.iter_mut() {
        let Some(delta) = choice.get_mut("delta").and_then(|d| d.as_object_mut()) else {
            continue;
        };
        // Only alias if the canonical name isn't already present —
        // we never want to overwrite a real reasoning_content with
        // a stale `reasoning` carried alongside it.
        if delta.contains_key("reasoning_content") {
            // If both keys are present (defensive — shouldn't happen
            // on the wire), drop the legacy one so consumers don't
            // see a duplicate. Mark touched so the caller
            // re-serialises from the mutated `parsed` value rather
            // than forwarding the unmodified input.
            if delta.remove("reasoning").is_some() {
                touched = true;
            }
            continue;
        }
        if let Some(r) = delta.remove("reasoning") {
            delta.insert("reasoning_content".to_string(), r);
            touched = true;
        }
    }
    touched
}

/// Non-streaming response variant — walks `choices[].message`.
pub(crate) fn normalize_reasoning_alias_in_choices(value: &mut Value) {
    let Some(choices) = value.get_mut("choices").and_then(|c| c.as_array_mut()) else {
        return;
    };
    for choice in choices.iter_mut() {
        let Some(message) = choice.get_mut("message").and_then(|m| m.as_object_mut()) else {
            continue;
        };
        if message.contains_key("reasoning_content") {
            message.remove("reasoning");
            continue;
        }
        if let Some(r) = message.remove("reasoning") {
            message.insert("reasoning_content".to_string(), r);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn groq_provider_name() {
        assert_eq!(GroqAdapter.provider_name(), "groq");
    }

    // groq_unsupported_params test removed — drop list moved to
    // the provider seed (covered by `services::providers` tests).

    #[test]
    fn needs_stream_normalization_is_true() {
        assert!(GroqAdapter.needs_stream_normalization());
    }

    #[test]
    fn rewrites_reasoning_to_reasoning_content_in_chunks() {
        let mut n = GroqStreamNormalizer;
        let chunk = r#"{"id":"x","choices":[{"index":0,"delta":{"reasoning":"thinking…"},"finish_reason":null}]}"#;
        let out = n.convert(chunk);
        assert_eq!(out.len(), 1);
        let v: Value = serde_json::from_str(&out[0]).unwrap();
        let delta = &v["choices"][0]["delta"];
        assert_eq!(delta["reasoning_content"], json!("thinking…"));
        assert!(delta.get("reasoning").is_none());
    }

    #[test]
    fn passes_through_content_chunks_unchanged() {
        let mut n = GroqStreamNormalizer;
        let chunk = r#"{"id":"x","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}"#;
        let out = n.convert(chunk);
        assert_eq!(out, vec![chunk.to_string()]);
    }

    #[test]
    fn passes_through_done_sentinel() {
        let mut n = GroqStreamNormalizer;
        assert_eq!(n.convert("[DONE]"), vec!["[DONE]".to_string()]);
    }

    #[test]
    fn passes_through_bad_json() {
        let mut n = GroqStreamNormalizer;
        // Non-JSON should not silently disappear.
        assert_eq!(n.convert("not-json"), vec!["not-json".to_string()]);
    }

    #[test]
    fn prefers_canonical_when_both_present() {
        // Defensive: if a future Groq chunk carried both fields we
        // keep the canonical and drop the legacy. Avoids the
        // downstream parser seeing two competing values.
        let mut n = GroqStreamNormalizer;
        let chunk = r#"{"id":"x","choices":[{"index":0,"delta":{"reasoning":"old","reasoning_content":"new"}}]}"#;
        let out = n.convert(chunk);
        let v: Value = serde_json::from_str(&out[0]).unwrap();
        let delta = &v["choices"][0]["delta"];
        assert_eq!(delta["reasoning_content"], json!("new"));
        assert!(delta.get("reasoning").is_none());
    }

    #[test]
    fn normalize_response_renames_message_reasoning() {
        let mut body = json!({
            "id": "x",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hi", "reasoning": "thought"},
                "finish_reason": "stop"
            }]
        });
        GroqAdapter.normalize_response(&mut body);
        assert_eq!(
            body["choices"][0]["message"]["reasoning_content"],
            json!("thought")
        );
        assert!(body["choices"][0]["message"].get("reasoning").is_none());
    }
}
